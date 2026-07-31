import logging
import os
import re
import secrets
import sqlite3
import string
from contextlib import contextmanager
from ipaddress import AddressValueError, IPv4Address
from types import SimpleNamespace


MAC_ADDRESS_RE = re.compile(r'^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$')
CONNECTION_HISTORY_DAYS = 30
MAX_CLOSED_CONNECTIONS = 2000
DISCONNECT_CONFIRMATION_POLLS = 2
# How stale a connected device's last_seen_at may get before it is rewritten.
# The dashboard shows this at minute granularity, so refreshing it on every
# few-second poll buys no visible accuracy and costs a disk write per device.
LAST_SEEN_REFRESH_SECONDS = 60


class UserManager:
    def __init__(self, db_path='config/piso_wifi.db'):
        self.db_path = db_path
        self.logger = logging.getLogger(__name__)
        # Per-deduction INFO logging floods journald (one line per device per
        # minute). Disable with LOG_DEDUCTIONS=false; the time_logs table keeps
        # the authoritative audit trail either way.
        self.log_deductions = os.getenv(
            'LOG_DEDUCTIONS', 'true').strip().lower() in ('1', 'true', 'yes', 'on')

        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        self._init_db()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError as exc:
            self.logger.warning("Could not restrict database permissions: %s", exc)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # SD-card friendly settings. WAL is a persistent property of the DB
        # file (set once in _init_db, not re-issued here), so every commit
        # appends to the WAL instead of rewriting a journal. NORMAL skips
        # redundant fsyncs (still durable under WAL) and busy_timeout prevents
        # 'database is locked' now that the server is threaded - both are
        # per-connection and must be set each time.
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA busy_timeout=5000')
        return conn

    @contextmanager
    def _with_conn(self, description, default=None, on_error=None):
        """Open a connection, commit on success, roll back and log on error.

        Yields a holder object; handlers set holder.result. On any exception
        the error is logged as '<description> failed' and holder.result is
        replaced with `default` (or on_error() when given).
        """
        conn = self._connect()
        holder = SimpleNamespace(result=default)
        try:
            yield conn, holder
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            self.logger.error(f"{description} failed: {exc}")
            holder.result = on_error() if on_error else default
        finally:
            conn.close()

    def _init_db(self):
        """Initialize database tables (additive migrations only)."""
        conn = self._connect()
        # WAL persists in the DB file header; setting it once here is enough
        # for every later connection to inherit it.
        conn.execute('PRAGMA journal_mode=WAL')
        c = conn.cursor()
        try:
            c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mac_address TEXT UNIQUE,
                    time_balance REAL DEFAULT 0,
                    status TEXT DEFAULT 'inactive',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_deduction TIMESTAMP,
                    download_limit INTEGER DEFAULT 1024,
                    upload_limit INTEGER DEFAULT 512,
                    plan TEXT DEFAULT 'default',
                    upgrade_requested BOOLEAN DEFAULT 0
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount REAL,
                    minutes INTEGER,
                    source TEXT DEFAULT 'cash',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS time_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    mac_address TEXT,
                    minutes_deducted REAL,
                    balance_before REAL,
                    balance_after REAL,
                    deducted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    deduction_type TEXT DEFAULT 'auto',
                    FOREIGN KEY (user_id) REFERENCES users (id)
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS plans (
                    name TEXT PRIMARY KEY,
                    download_kbps INTEGER NOT NULL,
                    upload_kbps INTEGER NOT NULL
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS vouchers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT UNIQUE NOT NULL,
                    minutes REAL NOT NULL,
                    price REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    redeemed_by TEXT,
                    redeemed_at TIMESTAMP
                )
            ''')

            # Persisted deduction clock so restarts don't grant free minutes
            c.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    mac_address TEXT PRIMARY KEY,
                    last_deduction_at REAL NOT NULL
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Advertisement posts shown in the portal/dashboard carousel
            c.execute('''
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    image_file TEXT NOT NULL,
                    active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Pricing tiers: pesos -> minutes
            c.execute('''
                CREATE TABLE IF NOT EXISTS rates (
                    pesos INTEGER PRIMARY KEY,
                    minutes REAL NOT NULL
                )
            ''')

            c.execute('''
                CREATE TABLE IF NOT EXISTS device_connections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mac_address TEXT NOT NULL,
                    hostname TEXT NOT NULL DEFAULT '',
                    ip_address TEXT,
                    connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    missed_polls INTEGER NOT NULL DEFAULT 0,
                    disconnected_at TIMESTAMP
                )
            ''')
            c.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_device_connections_open
                ON device_connections (mac_address)
                WHERE disconnected_at IS NULL
            ''')
            c.execute('''
                CREATE INDEX IF NOT EXISTS idx_device_connections_disconnected
                ON device_connections (disconnected_at)
            ''')
            c.execute('''
                CREATE INDEX IF NOT EXISTS idx_device_connections_latest
                ON device_connections
                    (mac_address, disconnected_at DESC, id DESC)
            ''')

            # The dashboard polls revenue on a timer and the history view sorts
            # by recency; without these both full-scan a table that grows with
            # every coin inserted.
            c.execute('''
                CREATE INDEX IF NOT EXISTS idx_transactions_created_at
                ON transactions (created_at DESC)
            ''')
            c.execute('''
                CREATE INDEX IF NOT EXISTS idx_time_logs_deducted_at
                ON time_logs (deducted_at)
            ''')

            # Duration passes ("valid for 30 days from redemption"). expires_at
            # is the authority for these devices; time_balance is kept in sync
            # with it so every existing balance check, block and display keeps
            # working unchanged. paused_at lets a pause push the deadline back.
            self._add_column_if_missing(c, 'users', 'expires_at', 'TIMESTAMP')
            self._add_column_if_missing(c, 'users', 'paused_at', 'TIMESTAMP')
            self._add_column_if_missing(
                c, 'users', 'pausable', 'INTEGER NOT NULL DEFAULT 1')
            self._add_column_if_missing(c, 'vouchers', 'duration_days', 'REAL')
            self._add_column_if_missing(
                c, 'vouchers', 'pausable', 'INTEGER NOT NULL DEFAULT 1')

            # Additive column migrations for databases created by older versions
            self._add_column_if_missing(c, 'transactions', 'source', "TEXT DEFAULT 'cash'")
            self._add_column_if_missing(c, 'vouchers', 'price', 'REAL DEFAULT 0')
            self._add_column_if_missing(
                c, 'device_connections', 'missed_polls',
                'INTEGER NOT NULL DEFAULT 0')
            self._add_column_if_missing(
                c, 'users', 'paused', 'INTEGER NOT NULL DEFAULT 0')

            # Seed plans
            c.execute('''INSERT OR IGNORE INTO plans (name, download_kbps, upload_kbps)
                         VALUES ('default', 2048, 1024), ('premium', 8096, 8096)''')

            # Seed rates only when the table is empty, so tiers an admin
            # deleted stay deleted across restarts
            if c.execute('SELECT COUNT(*) FROM rates').fetchone()[0] == 0:
                from pricing import DEFAULT_RATES
                c.executemany('INSERT INTO rates (pesos, minutes) VALUES (?, ?)',
                              sorted(DEFAULT_RATES.items()))

            conn.commit()
        except Exception as e:
            self.logger.error(f"Error initializing database: {e}")
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _add_column_if_missing(cursor, table, column, definition):
        cols = [row[1] for row in cursor.execute(f"PRAGMA table_info({table})")]
        if column not in cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # --- connection history -------------------------------------------------

    def _normalize_connection_device(self, device):
        mac = str(device.get('mac_address', '')).strip().upper()
        if not MAC_ADDRESS_RE.fullmatch(mac):
            self.logger.warning("Ignoring invalid connection-history MAC %r", mac)
            return None
        hostname = ''.join(
            character for character in str(device.get('hostname') or '')
            if character.isprintable()).strip()[:255]
        try:
            ip_address = str(IPv4Address(str(device.get('ip') or '').strip()))
        except AddressValueError:
            ip_address = None
        return mac, {'hostname': hostname, 'ip_address': ip_address}

    def sync_connection_snapshot(self, devices):
        """Persist one open session per present MAC and close absent sessions.

        Called on every discovery poll, so it is written to touch the disk only
        when something actually changed. A steady room of connected phones
        produces zero writes between last_seen refreshes; blindly stamping
        last_seen_at every poll dirtied a page per device per poll, which on an
        SD-card-rooted Pi is the single largest source of continuous wear.
        Retention trimming lives in prune_history(), on an hourly schedule.
        """
        normalized = [
            self._normalize_connection_device(dict(device))
            for device in devices
        ]
        devices_by_mac = dict(item for item in normalized if item is not None)
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            open_rows = {
                row['mac_address']: row
                for row in conn.execute('''
                    SELECT mac_address, hostname, ip_address, missed_polls,
                           (last_seen_at <= datetime('now', ?)) AS stale
                    FROM device_connections
                    WHERE disconnected_at IS NULL
                ''', (f'-{LAST_SEEN_REFRESH_SECONDS} seconds',))
            }

            for mac, device in devices_by_mac.items():
                existing = open_rows.get(mac)
                if existing is None:
                    conn.execute('''
                        INSERT INTO device_connections
                            (mac_address, hostname, ip_address)
                        VALUES (?, ?, ?)
                    ''', (mac, device['hostname'], device['ip_address']))
                elif (existing['missed_polls'] or existing['stale']
                        or existing['hostname'] != device['hostname']
                        or existing['ip_address'] != device['ip_address']):
                    conn.execute('''
                        UPDATE device_connections
                        SET hostname = ?, ip_address = ?,
                            last_seen_at = CURRENT_TIMESTAMP, missed_polls = 0
                        WHERE mac_address = ? AND disconnected_at IS NULL
                    ''', (device['hostname'], device['ip_address'], mac))

            absent_macs = tuple(
                mac for mac in open_rows if mac not in devices_by_mac)
            if absent_macs:
                placeholders = ','.join('?' for _ in absent_macs)
                conn.execute(f'''
                    UPDATE device_connections
                    SET missed_polls = missed_polls + 1
                    WHERE disconnected_at IS NULL
                      AND mac_address IN ({placeholders})
                ''', absent_macs)
                conn.execute(f'''
                    UPDATE device_connections
                    SET disconnected_at = CURRENT_TIMESTAMP
                    WHERE disconnected_at IS NULL
                      AND missed_polls >= ?
                      AND mac_address IN ({placeholders})
                ''', (DISCONNECT_CONFIRMATION_POLLS, *absent_macs))

            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            self.logger.error("Could not sync connection history: %s", exc)
            return False
        finally:
            conn.close()

    def prune_history(self, time_log_days=CONNECTION_HISTORY_DAYS):
        """Trim the append-only history tables. Hourly housekeeping, not a
        per-poll cost: the closed-connection cap sorts the whole closed set,
        which has no business running every few seconds.

        time_logs is write-only audit data that grows by one row per device
        per minute - left alone it is the fastest-growing table in the DB.
        """
        with self._with_conn('Pruning history', default=0) as (conn, out):
            removed = conn.execute('''
                DELETE FROM device_connections
                WHERE disconnected_at IS NOT NULL
                  AND disconnected_at < datetime('now', ?)
            ''', (f'-{CONNECTION_HISTORY_DAYS} days',)).rowcount
            removed += conn.execute('''
                DELETE FROM device_connections
                WHERE disconnected_at IS NOT NULL
                  AND id NOT IN (
                      SELECT id FROM device_connections
                      WHERE disconnected_at IS NOT NULL
                      ORDER BY disconnected_at DESC, id DESC
                      LIMIT ?
                  )
            ''', (MAX_CLOSED_CONNECTIONS,)).rowcount
            if time_log_days > 0:
                removed += conn.execute('''
                    DELETE FROM time_logs WHERE deducted_at < datetime('now', ?)
                ''', (f'-{int(time_log_days)} days',)).rowcount
            out.result = max(removed, 0)

        if out.result:
            self.logger.info(f"Pruned {out.result} history row(s)")
        return out.result

    def get_disconnected_devices(self, limit=100):
        safe_limit = max(1, min(int(limit), 500))
        conn = self._connect()
        try:
            rows = conn.execute('''
                SELECT dc.mac_address, dc.hostname, dc.ip_address,
                       datetime(dc.connected_at, 'localtime') AS connected_at,
                       datetime(dc.last_seen_at, 'localtime') AS last_seen_at,
                       datetime(dc.disconnected_at, 'localtime')
                           AS disconnected_at
                FROM device_connections AS dc
                WHERE dc.disconnected_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM device_connections AS open_session
                      WHERE open_session.mac_address = dc.mac_address
                        AND open_session.disconnected_at IS NULL
                  )
                  AND dc.id = (
                      SELECT closed_session.id
                      FROM device_connections AS closed_session
                      WHERE closed_session.mac_address = dc.mac_address
                        AND closed_session.disconnected_at IS NOT NULL
                      ORDER BY closed_session.disconnected_at DESC,
                               closed_session.id DESC
                      LIMIT 1
                  )
                ORDER BY dc.disconnected_at DESC, dc.id DESC
                LIMIT ?
            ''', (safe_limit,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # --- dynamic app settings ------------------------------------------------

    def get_app_settings(self, defaults):
        with self._with_conn('Loading app settings',
                             on_error=lambda: dict(defaults)) as (conn, out):
            rows = conn.execute('SELECT key, value FROM app_settings').fetchall()
            stored = {row['key']: row['value'] for row in rows}
            out.result = {**defaults, **stored}
        return out.result

    def update_app_settings(self, values):
        with self._with_conn('Saving app settings',
                             default=False) as (conn, out):
            for key, value in values.items():
                conn.execute('''
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                ''', (key, str(value)))
            out.result = True
        return out.result

    # --- advertisement posts -----------------------------------------------------

    def get_posts(self, active_only=False):
        with self._with_conn('Listing posts', default=[]) as (conn, out):
            # Timestamps are stored in UTC; render in the Pi's local timezone.
            query = ('SELECT id, title, description, image_file, active, '
                     "datetime(created_at, 'localtime') AS created_at "
                     'FROM posts')
            if active_only:
                query += ' WHERE active = 1'
            query += ' ORDER BY created_at DESC, id DESC'
            out.result = [dict(r) for r in conn.execute(query).fetchall()]
        return out.result

    def create_post(self, title, description, image_file, active=True):
        with self._with_conn('Creating post', default=False) as (conn, out):
            conn.execute(
                'INSERT INTO posts (title, description, image_file, active) '
                'VALUES (?, ?, ?, ?)',
                (title, description, image_file, 1 if active else 0))
            out.result = True
        return out.result

    def set_post_active(self, post_id, active):
        with self._with_conn(f'Updating post {post_id}',
                             default=False) as (conn, out):
            cursor = conn.execute('UPDATE posts SET active = ? WHERE id = ?',
                                  (1 if active else 0, post_id))
            out.result = cursor.rowcount > 0
        return out.result

    def update_post_description(self, post_id, description):
        with self._with_conn(f'Updating post description {post_id}',
                             default=False) as (conn, out):
            cursor = conn.execute(
                'UPDATE posts SET description = ? WHERE id = ?',
                (description, post_id))
            out.result = cursor.rowcount > 0
        return out.result

    def delete_post(self, post_id):
        """Delete a post; returns its image_file so the caller can remove it."""
        with self._with_conn(f'Deleting post {post_id}') as (conn, out):
            row = conn.execute('SELECT image_file FROM posts WHERE id = ?',
                               (post_id,)).fetchone()
            if row:
                conn.execute('DELETE FROM posts WHERE id = ?', (post_id,))
                out.result = row['image_file']
        return out.result

    # --- pricing tiers ---------------------------------------------------------

    def get_rates(self):
        """Pricing tiers as {pesos: minutes}, ascending by pesos."""
        with self._with_conn('Listing rates', default={}) as (conn, out):
            rows = conn.execute(
                'SELECT pesos, minutes FROM rates ORDER BY pesos').fetchall()
            out.result = {row['pesos']: row['minutes'] for row in rows}
        return out.result

    def upsert_rate(self, pesos, minutes):
        with self._with_conn(f'Saving rate ₱{pesos}',
                             default=False) as (conn, out):
            conn.execute('''
                INSERT INTO rates (pesos, minutes) VALUES (?, ?)
                ON CONFLICT(pesos) DO UPDATE SET minutes = excluded.minutes
            ''', (pesos, minutes))
            out.result = True
        return out.result

    def delete_rate(self, pesos):
        with self._with_conn(f'Deleting rate ₱{pesos}',
                             default=False) as (conn, out):
            conn.execute('DELETE FROM rates WHERE pesos = ?', (pesos,))
            out.result = True
        return out.result

    # --- balance / time -----------------------------------------------------

    def add_time(self, mac_address, amount, minutes, source='cash'):
        conn = self._connect()
        c = conn.cursor()
        try:
            c.execute('SELECT id FROM users WHERE mac_address = ?', (mac_address,))
            user = c.fetchone()

            if user is None:
                plan = self.get_plans().get('default', {
                    'download_kbps': 2048,
                    'upload_kbps': 1024,
                })
                c.execute('''
                    INSERT INTO users (
                        mac_address, time_balance, status,
                        download_limit, upload_limit, plan
                    ) VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    mac_address, minutes, 'active',
                    plan['download_kbps'], plan['upload_kbps'], 'default',
                ))
                user_id = c.lastrowid
            else:
                user_id = user['id']
                c.execute('UPDATE users SET time_balance = time_balance + ?, status = ? WHERE id = ?',
                          (minutes, 'active', user_id))

            c.execute('INSERT INTO transactions (user_id, amount, minutes, source) VALUES (?, ?, ?, ?)',
                      (user_id, amount, minutes, source))

            conn.commit()
            self.logger.info(f"Added {minutes} minutes for MAC {mac_address} ({source})")
            return True
        except Exception as e:
            self.logger.error(f"Error adding time: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def check_balance(self, mac_address):
        with self._with_conn('Checking balance', default=0) as (conn, out):
            row = conn.execute('SELECT time_balance FROM users WHERE mac_address = ?',
                               (mac_address,)).fetchone()
            out.result = row['time_balance'] if row else 0
        return out.result

    def deduct_time(self, mac_address, minutes, manual=False):
        """Deduct time (fractional minutes allowed) and log the deduction."""
        conn = self._connect()
        c = conn.cursor()
        try:
            c.execute('SELECT id, time_balance FROM users WHERE mac_address = ?', (mac_address,))
            result = c.fetchone()
            if not result:
                self.logger.warning(f"No user found for MAC {mac_address}")
                return False

            user_id, current_balance = result['id'], result['time_balance']
            # round() keeps repeated fractional deductions from accumulating
            # float dust (e.g. 47.54999999999998) in the stored balance
            new_balance = round(max(0, current_balance - minutes), 2)

            c.execute('''
                UPDATE users
                SET time_balance = ?,
                    status = CASE WHEN ? <= 0 THEN 'inactive' ELSE 'active' END,
                    last_deduction = CURRENT_TIMESTAMP
                WHERE mac_address = ?
            ''', (new_balance, new_balance, mac_address))

            c.execute('''
                INSERT INTO time_logs (user_id, mac_address, minutes_deducted,
                                       balance_before, balance_after, deducted_at, deduction_type)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ''', (user_id, mac_address, minutes, current_balance, new_balance,
                  'manual' if manual else 'auto'))

            conn.commit()
            if self.log_deductions:
                self.logger.info(
                    f"Deducted {minutes} minutes from {mac_address}. "
                    f"Balance: {current_balance} -> {new_balance}")
            return True
        except Exception as e:
            self.logger.error(f"Error deducting time: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    # --- duration passes ------------------------------------------------

    def grant_duration(self, mac_address, days, pausable=True, source='voucher'):
        """Give a device a pass valid for `days` from now.

        Stacking extends an unexpired pass rather than truncating it. The
        wall-clock deadline is the authority; time_balance is mirrored from it
        by sync_expiring_devices() so the portal countdown, the zero-balance
        block and the idle-device purge all keep working untouched.
        """
        mac_address = (mac_address or '').strip().upper()
        if days <= 0:
            return None

        with self._with_conn(f'Granting {days}d pass to {mac_address}') as (conn, out):
            row = conn.execute(
                'SELECT id, expires_at FROM users WHERE mac_address = ?',
                (mac_address,)).fetchone()
            if row is None:
                plan = self.get_plans().get('default', {
                    'download_kbps': 2048, 'upload_kbps': 1024})
                conn.execute('''
                    INSERT INTO users (mac_address, time_balance, status,
                                       download_limit, upload_limit, plan)
                    VALUES (?, 0, 'active', ?, ?, 'default')
                ''', (mac_address, plan['download_kbps'], plan['upload_kbps']))

            # Extend from whichever is later: now, or an unexpired deadline.
            conn.execute('''
                UPDATE users
                SET expires_at = datetime(
                        MAX(COALESCE(expires_at, datetime('now')), datetime('now')),
                        '+' || ? || ' seconds'),
                    pausable = ?,
                    paused = 0,
                    paused_at = NULL,
                    status = 'active'
                WHERE mac_address = ?
            ''', (int(float(days) * 86400), 1 if pausable else 0, mac_address))

            deadline = conn.execute(
                'SELECT expires_at FROM users WHERE mac_address = ?',
                (mac_address,)).fetchone()['expires_at']
            conn.execute('''
                UPDATE users
                SET time_balance = MAX(0, ROUND(
                        (julianday(expires_at) - julianday('now')) * 1440.0, 2))
                WHERE mac_address = ?
            ''', (mac_address,))
            conn.execute('INSERT INTO transactions '
                         '(user_id, amount, minutes, source) VALUES '
                         '((SELECT id FROM users WHERE mac_address = ?), 0, 0, ?)',
                         (mac_address, source))
            out.result = deadline

        if out.result:
            # A fresh pass must not be back-charged by the elapsed-time meter
            self.clear_session(mac_address)
            self.logger.info(
                f"Granted {days}d pass to {mac_address} (until {out.result}, "
                f"pausable={bool(pausable)})")
        return out.result

    def sync_expiring_devices(self):
        """Mirror expires_at into time_balance for every duration pass.

        One statement for all of them rather than a write per device, and the
        caller runs it on a slow cadence - a wall-clock deadline needs no
        finer resolution than the minute the portal displays.

        Returns (tracked_macs, expired_macs).
        """
        with self._with_conn('Syncing duration passes',
                             default=([], [])) as (conn, out):
            conn.execute('''
                UPDATE users
                SET time_balance = MAX(0, ROUND(
                        (julianday(expires_at) - julianday('now')) * 1440.0, 2)),
                    status = CASE
                        WHEN julianday(expires_at) > julianday('now')
                        THEN 'active' ELSE 'inactive' END
                WHERE expires_at IS NOT NULL AND paused = 0
            ''')
            rows = conn.execute('''
                SELECT mac_address, time_balance
                FROM users WHERE expires_at IS NOT NULL
            ''').fetchall()
            out.result = (
                [row['mac_address'] for row in rows],
                [row['mac_address'] for row in rows
                 if row['time_balance'] <= 0],
            )
        return out.result

    def get_expiry(self, mac_address):
        """Local-time deadline string for a duration pass, or None."""
        with self._with_conn('Reading pass expiry') as (conn, out):
            row = conn.execute(
                "SELECT datetime(expires_at, 'localtime') AS expires_at "
                'FROM users WHERE mac_address = ? AND expires_at IS NOT NULL',
                (mac_address,)).fetchone()
            out.result = row['expires_at'] if row else None
        return out.result

    def is_pausable(self, mac_address):
        """False only when the device's current pass forbids pausing."""
        with self._with_conn('Reading pausable flag', default=True) as (conn, out):
            row = conn.execute(
                'SELECT pausable FROM users WHERE mac_address = ?',
                (mac_address,)).fetchone()
            out.result = bool(row['pausable']) if row else True
        return out.result

    def set_pausable(self, mac_address, pausable):
        """Override whether this device's current pass may be paused.

        grant_duration() sets this at redemption from the voucher's own flag;
        this is the after-the-fact correction for a pass that was sold with the
        wrong setting. Returns False when the MAC is unknown, so the caller can
        tell "no such device" apart from a successful change.

        Revoking permission deliberately does NOT resume a device that is
        already paused: that would hand out internet and restart the customer's
        clock without warning. The portal keeps Resume reachable while paused
        regardless of this flag, so nobody is stranded with a frozen balance.
        """
        with self._with_conn('Updating pausable flag',
                             default=False) as (conn, out):
            cursor = conn.execute(
                'UPDATE users SET pausable = ? WHERE mac_address = ?',
                (1 if pausable else 0, mac_address))
            out.result = cursor.rowcount > 0
        if out.result:
            self.logger.info("Set pausable=%s for %s",
                             bool(pausable), mac_address)
        return out.result

    def transfer_balance(self, from_mac, to_mac):
        """Move a device's remaining time to another MAC.

        Needed because balances are keyed on MAC: a phone that rotates its
        randomized Wi-Fi address (iOS "Rotating" private address, Android
        non-persistent randomization, or a plain "Forget This Network")
        rejoins as a brand-new device and loses its time.

        Returns the minutes moved, or None when the source has no balance.
        """
        from_mac = (from_mac or '').strip().upper()
        to_mac = (to_mac or '').strip().upper()
        if from_mac == to_mac:
            return None

        with self._with_conn(
                f'Transferring balance {from_mac} -> {to_mac}') as (conn, out):
            source = conn.execute(
                'SELECT id, time_balance, expires_at, pausable '
                'FROM users WHERE mac_address = ?',
                (from_mac,)).fetchone()
            if source is not None and source['time_balance'] > 0:
                minutes = source['time_balance']
                target = conn.execute(
                    'SELECT id FROM users WHERE mac_address = ?',
                    (to_mac,)).fetchone()

                if target is None:
                    # No row for the new MAC yet: rename in place so the
                    # device keeps its plan, bandwidth limits and history.
                    conn.execute(
                        'UPDATE users SET mac_address = ? WHERE id = ?',
                        (to_mac, source['id']))
                else:
                    # Carry any duration pass across too, or a monthly
                    # customer whose MAC rotated would be handed loose
                    # minutes that the elapsed-time meter then drains. The
                    # target keeps its own deadline if it is already later.
                    conn.execute('''
                        UPDATE users
                        SET time_balance = time_balance + ?,
                            status = 'active',
                            expires_at = CASE
                                WHEN ? IS NOT NULL
                                     AND (expires_at IS NULL OR expires_at < ?)
                                THEN ? ELSE expires_at END,
                            pausable = CASE WHEN ? IS NOT NULL
                                THEN ? ELSE pausable END
                        WHERE id = ?
                    ''', (minutes, source['expires_at'], source['expires_at'],
                          source['expires_at'], source['expires_at'],
                          source['pausable'], target['id']))
                    conn.execute('''
                        UPDATE users
                        SET time_balance = 0, status = 'inactive',
                            expires_at = NULL, paused_at = NULL
                        WHERE id = ?
                    ''', (source['id'],))

                out.result = minutes

        if out.result is not None:
            self.logger.info(
                f"Transferred {out.result} minutes from {from_mac} to {to_mac}")
        return out.result

    def purge_stale_devices(self, retention_hours=24):
        """Delete spent devices that have been idle for retention_hours.

        Rotated randomized MACs leave behind zero-balance user rows that will
        never be seen again; without this the users table grows forever.
        Transactions and time_logs are kept for the audit trail (revenue sums
        transactions directly, and the history view left-joins users, so a
        purged device just shows a blank MAC on rows older than the window).

        retention_hours <= 0 disables the purge. Returns the rows deleted.
        """
        if retention_hours <= 0:
            return 0

        cutoff = f'-{int(retention_hours)} hours'
        with self._with_conn('Purging stale devices', default=0) as (conn, out):
            rows = conn.execute('''
                SELECT id, mac_address FROM users
                WHERE time_balance <= 0
                  AND COALESCE(last_deduction, created_at) < datetime('now', ?)
                  AND id NOT IN (
                      SELECT user_id FROM transactions
                      WHERE user_id IS NOT NULL
                        AND created_at >= datetime('now', ?)
                  )
            ''', (cutoff, cutoff)).fetchall()

            if rows:
                ids = [row['id'] for row in rows]
                macs = [row['mac_address'] for row in rows]
                placeholders = ','.join('?' for _ in ids)
                conn.execute(
                    f'DELETE FROM users WHERE id IN ({placeholders})', ids)
                conn.execute(
                    f'DELETE FROM sessions WHERE mac_address IN ({placeholders})',
                    macs)
                self.logger.info(
                    f"Purged {len(rows)} device(s) idle for over "
                    f"{int(retention_hours)}h")

            out.result = len(rows)
        return out.result

    # --- device / plan info ---------------------------------------------------

    def get_device_info(self, mac_address):
        with self._with_conn('Getting device info') as (conn, out):
            row = conn.execute('''
                SELECT time_balance, status, download_limit, upload_limit,
                       plan, upgrade_requested, paused
                FROM users WHERE mac_address = ?
            ''', (mac_address,)).fetchone()
            out.result = dict(row) if row else None
        return out.result

    def get_devices_info(self, mac_addresses):
        """Device info for many MACs in one query: {mac: info}."""
        macs = [mac.upper() for mac in mac_addresses]
        if not macs:
            return {}
        with self._with_conn('Getting devices info',
                             default={}) as (conn, out):
            placeholders = ','.join('?' for _ in macs)
            rows = conn.execute(f'''
                SELECT mac_address, time_balance, status, download_limit,
                       upload_limit, plan, upgrade_requested
                FROM users WHERE mac_address IN ({placeholders})
            ''', macs).fetchall()
            out.result = {
                row['mac_address']: {
                    key: row[key] for key in row.keys()
                    if key != 'mac_address'
                }
                for row in rows
            }
        return out.result

    def get_active_users(self):
        """Users with remaining balance - used to reconcile network rules."""
        with self._with_conn('Listing active users',
                             default=[]) as (conn, out):
            rows = conn.execute('''
                SELECT mac_address, download_limit, upload_limit
                FROM users WHERE time_balance > 0
            ''').fetchall()
            out.result = [dict(r) for r in rows]
        return out.result

    def get_users_with_balance(self):
        """Devices with remaining time, enriched with their latest connection
        record (hostname / last IP / last seen), highest balance first."""
        with self._with_conn('Listing users with balance',
                             default=[]) as (conn, out):
            rows = conn.execute('''
                SELECT u.mac_address, u.time_balance, u.plan, u.paused,
                       u.pausable,
                       datetime(u.expires_at, 'localtime') AS expires_at,
                       dc.hostname, dc.ip_address,
                       datetime(dc.last_seen_at, 'localtime') AS last_seen_at
                FROM users u
                LEFT JOIN device_connections dc ON dc.id = (
                    SELECT id FROM device_connections
                    WHERE mac_address = u.mac_address
                    ORDER BY last_seen_at DESC, id DESC
                    LIMIT 1
                )
                WHERE u.time_balance > 0
                ORDER BY u.time_balance DESC
            ''').fetchall()
            out.result = [dict(r) for r in rows]
        return out.result

    def request_upgrade(self, mac_address):
        with self._with_conn('Requesting upgrade',
                             default=False) as (conn, out):
            conn.execute('UPDATE users SET upgrade_requested = 1 WHERE mac_address = ?',
                         (mac_address,))
            out.result = True
        return out.result

    def get_plans(self):
        with self._with_conn('Listing plans', default={}) as (conn, out):
            rows = conn.execute(
                'SELECT name, download_kbps, upload_kbps FROM plans').fetchall()
            out.result = {r['name']: dict(r) for r in rows}
        return out.result

    def upsert_plan(self, name, download_kbps, upload_kbps):
        with self._with_conn(f'Saving plan {name}',
                             default=False) as (conn, out):
            conn.execute('''
                INSERT INTO plans (name, download_kbps, upload_kbps) VALUES (?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET download_kbps = excluded.download_kbps,
                                                upload_kbps = excluded.upload_kbps
            ''', (name, download_kbps, upload_kbps))
            conn.execute('''
                UPDATE users
                SET download_limit = ?, upload_limit = ?
                WHERE plan = ?
            ''', (download_kbps, upload_kbps, name))
            out.result = True
        return out.result

    def set_plan(self, mac_address, plan_name):
        """Assign a plan; returns (download_kbps, upload_kbps) or None."""
        plan = self.get_plans().get(plan_name)
        if not plan:
            self.logger.error(f"Unknown plan: {plan_name}")
            return None
        with self._with_conn('Setting plan') as (conn, out):
            conn.execute('''
                UPDATE users
                SET plan = ?, download_limit = ?, upload_limit = ?, upgrade_requested = 0
                WHERE mac_address = ?
            ''', (plan_name, plan['download_kbps'], plan['upload_kbps'], mac_address))
            out.result = (plan['download_kbps'], plan['upload_kbps'])
        return out.result

    def set_bandwidth(self, mac_address, download_kbps, upload_kbps):
        with self._with_conn('Setting bandwidth',
                             default=False) as (conn, out):
            conn.execute('''
                INSERT INTO users (
                    mac_address, time_balance, status,
                    download_limit, upload_limit, plan
                ) VALUES (?, 0, 'inactive', ?, ?, 'custom')
                ON CONFLICT(mac_address) DO UPDATE SET
                    download_limit = excluded.download_limit,
                    upload_limit = excluded.upload_limit,
                    plan = 'custom'
            ''', (mac_address, download_kbps, upload_kbps))
            out.result = True
        return out.result

    # --- vouchers -------------------------------------------------------------

    def create_voucher(self, minutes, price=0, duration_days=None, pausable=True):
        """Create a voucher worth the given minutes; returns the code.

        duration_days turns it into a pass instead: redeeming stamps a
        wall-clock deadline that many days out, rather than crediting minutes.
        pausable=False means that pass ignores the portal's pause button.

        price > 0 marks a paid voucher: the sale is recorded as revenue at
        creation time (cash changed hands when the voucher was sold), in the
        same transaction as the voucher insert. Redemption stays amount=0 so
        the sale is never double-counted.
        """
        alphabet = string.ascii_uppercase + string.digits
        conn = self._connect()
        try:
            for _ in range(10):
                code = '-'.join(
                    ''.join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))
                try:
                    conn.execute(
                        'INSERT INTO vouchers '
                        '(code, minutes, price, duration_days, pausable) '
                        'VALUES (?, ?, ?, ?, ?)',
                        (code, minutes, price, duration_days,
                         1 if pausable else 0))
                    if price > 0:
                        conn.execute('''
                            INSERT INTO transactions
                                (user_id, amount, minutes, source)
                            VALUES (NULL, ?, ?, 'voucher')
                        ''', (price, minutes))
                    conn.commit()
                    self.logger.info(
                        f"Created voucher {code} worth {minutes} minutes"
                        f" (price ₱{price:g})" if price else
                        f"Created voucher {code} worth {minutes} minutes")
                    return code
                except sqlite3.IntegrityError:
                    conn.rollback()
                    continue
            self.logger.error("Could not generate a unique voucher code")
            return None
        except Exception as e:
            self.logger.error(f"Error creating voucher: {e}")
            conn.rollback()
            return None
        finally:
            conn.close()

    def redeem_voucher(self, code, mac_address):
        """Redeem a voucher for a device.

        Returns a dict describing what was granted, or None when the code is
        unknown or already used:
            {'minutes': float, 'duration_days': float|None, 'expires_at': str|None}
        """
        conn = self._connect()
        try:
            row = conn.execute(
                'SELECT id, minutes, duration_days, pausable FROM vouchers '
                'WHERE code = ? AND redeemed_at IS NULL',
                (code.strip().upper(),)).fetchone()
            if not row:
                return None
            cursor = conn.execute('''
                UPDATE vouchers SET redeemed_by = ?, redeemed_at = CURRENT_TIMESTAMP
                WHERE id = ? AND redeemed_at IS NULL
            ''', (mac_address, row['id']))
            conn.commit()
            if cursor.rowcount != 1:
                return None
        except Exception as e:
            self.logger.error(f"Error redeeming voucher: {e}")
            conn.rollback()
            return None
        finally:
            conn.close()

        if row['duration_days']:
            deadline = self.grant_duration(
                mac_address, row['duration_days'], bool(row['pausable']))
            if deadline:
                return {'minutes': self.check_balance(mac_address),
                        'duration_days': row['duration_days'],
                        'expires_at': self.get_expiry(mac_address)}
            return None

        if self.add_time(mac_address, 0, row['minutes'], source='voucher'):
            return {'minutes': row['minutes'], 'duration_days': None,
                    'expires_at': None}
        return None

    def set_voucher_pausable(self, code, pausable):
        """Correct the pause permission on an already-created voucher.

        Returns {'found': bool, 'cascaded_to': mac or None}.

        When the voucher has already been redeemed the change is pushed to the
        device that redeemed it as well, in the same transaction. Fixing only
        the voucher row would be useless there: grant_duration() copied the
        flag onto the user at redemption, and users.pausable is what
        is_pausable() actually reads.
        """
        with self._with_conn(
                'Updating voucher pausable flag',
                on_error=lambda: {'found': False, 'cascaded_to': None},
        ) as (conn, out):
            out.result = {'found': False, 'cascaded_to': None}
            row = conn.execute(
                'SELECT redeemed_by FROM vouchers WHERE code = ?',
                (code,)).fetchone()
            if row is not None:
                flag = 1 if pausable else 0
                conn.execute('UPDATE vouchers SET pausable = ? WHERE code = ?',
                             (flag, code))
                redeemed_by = row['redeemed_by']
                if redeemed_by:
                    conn.execute(
                        'UPDATE users SET pausable = ? WHERE mac_address = ?',
                        (flag, redeemed_by))
                out.result = {'found': True, 'cascaded_to': redeemed_by}
                self.logger.info(
                    "Set voucher %s pausable=%s (device %s)",
                    code, bool(pausable), redeemed_by or 'unredeemed')
        return out.result

    def get_vouchers(self, include_redeemed=False):
        with self._with_conn('Listing vouchers', default=[]) as (conn, out):
            query = ("SELECT code, minutes, price, duration_days, pausable, "
                     "datetime(created_at, 'localtime') AS created_at, "
                     "redeemed_by, "
                     "datetime(redeemed_at, 'localtime') AS redeemed_at "
                     "FROM vouchers")
            if not include_redeemed:
                query += ' WHERE redeemed_at IS NULL'
            query += ' ORDER BY created_at DESC'
            out.result = [dict(r) for r in conn.execute(query).fetchall()]
        return out.result

    # --- transactions -----------------------------------------------------------

    def get_transactions(self, limit=50):
        with self._with_conn('Listing transactions',
                             default=[]) as (conn, out):
            rows = conn.execute('''
                SELECT t.amount, t.minutes, t.source,
                       datetime(t.created_at, 'localtime') AS created_at,
                       u.mac_address
                FROM transactions t LEFT JOIN users u ON u.id = t.user_id
                ORDER BY t.created_at DESC LIMIT ?
            ''', (limit,)).fetchall()
            out.result = [dict(r) for r in rows]
        return out.result

    def record_revenue_adjustment(self, amount):
        """Record a manual revenue correction as a negative transaction.

        amount is the positive peso figure to REMOVE from revenue (e.g. test
        coins from a grounded pulse). Stored as source='adjustment' with a
        negative amount so the revenue summary nets it out while keeping an
        audit trail. Returns True on success.
        """
        if amount <= 0:
            self.logger.error("Revenue adjustment must be positive: %r", amount)
            return False
        with self._with_conn('Recording revenue adjustment',
                             default=False) as (conn, out):
            conn.execute(
                'INSERT INTO transactions (user_id, amount, minutes, source) '
                "VALUES (NULL, ?, 0, 'adjustment')", (-abs(amount),))
            out.result = True
        return out.result

    def get_revenue_summary(self):
        with self._with_conn(
                'Calculating revenue summary',
                on_error=lambda: {
                    'day': 0.0, 'week': 0.0, 'month': 0.0,
                    'day_adjustments': 0.0, 'week_adjustments': 0.0,
                    'month_adjustments': 0.0,
                },
        ) as (conn, out):
            row = conn.execute('''
                SELECT
                    COALESCE(SUM(CASE
                        WHEN date(created_at, 'localtime') = date('now', 'localtime')
                        THEN amount ELSE 0
                    END), 0) AS day,
                    -- Calendar-aligned, like the day and month buckets:
                    -- seven whole days ending today. Cutting at the current
                    -- clock time six days back excluded that day's earlier
                    -- earnings while still counting the adjustments recorded
                    -- later the same evening, which drove the total negative
                    -- and made it drift with the hour the page was opened.
                    COALESCE(SUM(CASE
                        WHEN date(created_at, 'localtime')
                             >= date('now', 'localtime', '-6 days')
                        THEN amount ELSE 0
                    END), 0) AS week,
                    COALESCE(SUM(CASE
                        WHEN strftime('%Y-%m', created_at, 'localtime')
                             = strftime('%Y-%m', 'now', 'localtime')
                        THEN amount ELSE 0
                    END), 0) AS month,
                    -- Manual corrections as positive magnitudes, so a card
                    -- that nets out low or negative can explain why.
                    COALESCE(SUM(CASE
                        WHEN amount < 0 AND date(created_at, 'localtime')
                             = date('now', 'localtime')
                        THEN -amount ELSE 0
                    END), 0) AS day_adjustments,
                    COALESCE(SUM(CASE
                        WHEN amount < 0 AND date(created_at, 'localtime')
                             >= date('now', 'localtime', '-6 days')
                        THEN -amount ELSE 0
                    END), 0) AS week_adjustments,
                    COALESCE(SUM(CASE
                        WHEN amount < 0
                             AND strftime('%Y-%m', created_at, 'localtime')
                                 = strftime('%Y-%m', 'now', 'localtime')
                        THEN -amount ELSE 0
                    END), 0) AS month_adjustments
                FROM transactions
                WHERE amount != 0
                  -- Only rows that can land in one of the three buckets. The
                  -- dashboard re-runs this on a timer, and without a bound it
                  -- rescans every transaction ever recorded. Comparing the raw
                  -- UTC column (not datetime(created_at,'localtime')) is what
                  -- lets idx_transactions_created_at do the work.
                  -- 'start of day' must match the week bucket above, or on the
                  -- first days of a month this bound clips rows the week needs.
                  AND created_at >= datetime(MIN(
                        datetime('now', 'localtime', 'start of month'),
                        datetime('now', 'localtime', '-6 days',
                                 'start of day')), 'utc')
            ''').fetchone()
            out.result = {
                'day': float(row['day']),
                'week': float(row['week']),
                'month': float(row['month']),
                'day_adjustments': float(row['day_adjustments']),
                'week_adjustments': float(row['week_adjustments']),
                'month_adjustments': float(row['month_adjustments']),
            }
        return out.result

    def get_device_sessions(self, mac_address, limit=10):
        """Recent connect/disconnect history for one device.

        Backs the portal's Sessions sheet. Ordered newest first; an open
        session has disconnected_at NULL and renders as 'active'.
        """
        with self._with_conn('Listing device sessions',
                             default=[]) as (conn, out):
            rows = conn.execute('''
                SELECT datetime(connected_at, 'localtime') AS connected_at,
                       CASE WHEN disconnected_at IS NULL THEN NULL
                            ELSE datetime(disconnected_at, 'localtime')
                       END AS disconnected_at,
                       ip_address
                FROM device_connections
                WHERE mac_address = ?
                ORDER BY connected_at DESC
                LIMIT ?
            ''', (mac_address, limit)).fetchall()
            out.result = [dict(row) for row in rows]
        return out.result

    # --- sales reporting ----------------------------------------------------

    # Grouping expressions keyed by the report's group_by parameter. Kept as a
    # whitelist because the value is interpolated into SQL: it can never come
    # straight from the query string.
    _REPORT_GROUPING = {
        'day': "date(t.created_at, 'localtime')",
        'week': "strftime('%Y-W%W', t.created_at, 'localtime')",
        'month': "strftime('%Y-%m', t.created_at, 'localtime')",
    }

    @staticmethod
    def _range_clause():
        """Shared WHERE for range reports.

        Filters on the local date so a range means what the operator sees on
        screen, and adds a bound on the raw UTC column so
        idx_transactions_created_at still drives the scan.
        """
        return '''
            WHERE date(t.created_at, 'localtime') BETWEEN ? AND ?
              AND t.created_at >= datetime(? || ' 00:00:00', 'utc')
              AND t.created_at < datetime(? || ' 00:00:00', '+1 day', 'utc')
        '''

    @staticmethod
    def _range_params(start_date, end_date):
        return (start_date, end_date, start_date, end_date)

    def get_sales_report(self, start_date, end_date, group_by='day'):
        """Grouped sales for an inclusive local-date range.

        Returns {'buckets': [...], 'by_source': [...], 'totals': {...}}.
        Gross counts positive rows only, adjustments are the magnitude of the
        negative rows, and net is their sum - so a period whose corrections
        exceed its takings reads as negative rather than silently clamping.
        """
        empty = {'buckets': [], 'by_source': [],
                 'totals': {'gross': 0.0, 'adjustments': 0.0, 'net': 0.0,
                            'count': 0, 'minutes': 0}}
        if group_by not in self._REPORT_GROUPING:
            group_by = 'day'
        expression = self._REPORT_GROUPING[group_by]

        with self._with_conn('Building sales report',
                             on_error=lambda: dict(empty)) as (conn, out):
            where = self._range_clause()
            params = self._range_params(start_date, end_date)

            bucket_rows = conn.execute(f'''
                SELECT {expression} AS period,
                       COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.amount END), 0) AS gross,
                       COALESCE(SUM(CASE WHEN t.amount < 0 THEN -t.amount END), 0) AS adjustments,
                       COALESCE(SUM(t.amount), 0) AS net,
                       COALESCE(SUM(CASE WHEN t.amount > 0 THEN t.minutes END), 0) AS minutes,
                       COUNT(*) AS count
                FROM transactions t
                {where}
                GROUP BY period
                ORDER BY period ASC
            ''', params).fetchall()

            source_rows = conn.execute(f'''
                SELECT COALESCE(t.source, 'unknown') AS source,
                       COALESCE(SUM(t.amount), 0) AS net,
                       COUNT(*) AS count
                FROM transactions t
                {where}
                GROUP BY source
                ORDER BY net DESC
            ''', params).fetchall()

            buckets = [dict(row) for row in bucket_rows]
            out.result = {
                'buckets': buckets,
                'by_source': [dict(row) for row in source_rows],
                'totals': {
                    'gross': float(sum(b['gross'] for b in buckets)),
                    'adjustments': float(sum(b['adjustments'] for b in buckets)),
                    'net': float(sum(b['net'] for b in buckets)),
                    'count': int(sum(b['count'] for b in buckets)),
                    'minutes': int(sum(b['minutes'] for b in buckets)),
                },
            }
        return out.result

    def get_transactions_between(self, start_date, end_date, limit=10000):
        """Individual transactions in an inclusive local-date range.

        Feeds the CSV export, so it returns exactly the rows the on-screen
        report aggregates. The limit is a memory guard on an SBC, not a page
        size - the report warns when it truncates.
        """
        with self._with_conn('Listing transactions in range',
                             default=[]) as (conn, out):
            rows = conn.execute(f'''
                SELECT datetime(t.created_at, 'localtime') AS created_at,
                       COALESCE(u.mac_address, '') AS mac_address,
                       COALESCE(t.source, 'unknown') AS source,
                       t.amount, t.minutes
                FROM transactions t LEFT JOIN users u ON u.id = t.user_id
                {self._range_clause()}
                ORDER BY t.created_at ASC
                LIMIT ?
            ''', (*self._range_params(start_date, end_date), limit)).fetchall()
            out.result = [dict(row) for row in rows]
        return out.result

    def reset_revenue(self):
        """Zero all revenue by clearing the transactions ledger. Device
        balances, vouchers and time_logs are untouched. Returns the number of
        rows removed. Irreversible - the revenue audit trail is discarded.
        """
        with self._with_conn('Resetting revenue', default=0) as (conn, out):
            out.result = conn.execute('DELETE FROM transactions').rowcount
        self.logger.info("Revenue reset: cleared %s transaction row(s)", out.result)
        return out.result

    # --- session persistence (deduction clock) ----------------------------------

    def get_last_deduction(self, mac_address):
        conn = self._connect()
        try:
            row = conn.execute('SELECT last_deduction_at FROM sessions WHERE mac_address = ?',
                               (mac_address,)).fetchone()
            return row['last_deduction_at'] if row else None
        finally:
            conn.close()

    def set_last_deduction(self, mac_address, timestamp):
        conn = self._connect()
        try:
            conn.execute('''
                INSERT INTO sessions (mac_address, last_deduction_at) VALUES (?, ?)
                ON CONFLICT(mac_address) DO UPDATE SET last_deduction_at = excluded.last_deduction_at
            ''', (mac_address, timestamp))
            conn.commit()
        finally:
            conn.close()

    def clear_session(self, mac_address):
        conn = self._connect()
        try:
            conn.execute('DELETE FROM sessions WHERE mac_address = ?', (mac_address,))
            conn.commit()
        finally:
            conn.close()

    def set_paused(self, mac_address, paused):
        """Manually pause/resume a device's metering. Returns True on success.

        For a duration pass the deadline moves out by however long the device
        stayed paused, so the customer keeps the full span they paid for
        instead of watching it burn while their internet was off.
        """
        with self._with_conn('Setting pause state', default=False) as (conn, out):
            if paused:
                cursor = conn.execute('''
                    UPDATE users
                    SET paused = 1, paused_at = datetime('now')
                    WHERE mac_address = ?
                ''', (mac_address,))
            else:
                cursor = conn.execute('''
                    UPDATE users
                    SET paused = 0,
                        expires_at = CASE
                            WHEN expires_at IS NOT NULL AND paused_at IS NOT NULL
                            THEN datetime(expires_at, '+' ||
                                 (strftime('%s', 'now') - strftime('%s', paused_at))
                                 || ' seconds')
                            ELSE expires_at END,
                        paused_at = NULL
                    WHERE mac_address = ?
                ''', (mac_address,))
            out.result = cursor.rowcount > 0
        return out.result

    def is_paused(self, mac_address):
        with self._with_conn('Reading pause state', default=False) as (conn, out):
            row = conn.execute(
                'SELECT paused FROM users WHERE mac_address = ?',
                (mac_address,)).fetchone()
            out.result = bool(row['paused']) if row else False
        return out.result

    # --- health -------------------------------------------------------------------

    def check_health(self):
        try:
            conn = self._connect()
            conn.execute('SELECT 1')
            conn.close()
            return True
        except Exception as e:
            self.logger.error(f"Database health check failed: {e}")
            return False
