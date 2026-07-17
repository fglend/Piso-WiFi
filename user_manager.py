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


class UserManager:
    def __init__(self, db_path='config/piso_wifi.db'):
        self.db_path = db_path
        self.logger = logging.getLogger(__name__)

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
        # SD-card friendly settings: WAL avoids full journal rewrites on
        # every commit (the metering loop writes every few seconds), NORMAL
        # skips redundant fsyncs (still durable for WAL), and busy_timeout
        # prevents 'database is locked' now that the server is threaded.
        conn.execute('PRAGMA journal_mode=WAL')
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

            # Additive column migrations for databases created by older versions
            self._add_column_if_missing(c, 'transactions', 'source', "TEXT DEFAULT 'cash'")
            self._add_column_if_missing(c, 'vouchers', 'price', 'REAL DEFAULT 0')
            self._add_column_if_missing(
                c, 'device_connections', 'missed_polls',
                'INTEGER NOT NULL DEFAULT 0')

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
        """Persist one open session per present MAC and close absent sessions."""
        normalized = [
            self._normalize_connection_device(dict(device))
            for device in devices
        ]
        devices_by_mac = dict(item for item in normalized if item is not None)
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            for mac, device in devices_by_mac.items():
                cursor = conn.execute('''
                    UPDATE device_connections
                    SET hostname = ?, ip_address = ?,
                        last_seen_at = CURRENT_TIMESTAMP, missed_polls = 0
                    WHERE mac_address = ? AND disconnected_at IS NULL
                ''', (device['hostname'], device['ip_address'], mac))
                if cursor.rowcount == 0:
                    conn.execute('''
                        INSERT INTO device_connections
                            (mac_address, hostname, ip_address)
                        VALUES (?, ?, ?)
                    ''', (mac, device['hostname'], device['ip_address']))

            present_macs = tuple(devices_by_mac)
            if present_macs:
                placeholders = ','.join('?' for _ in present_macs)
                conn.execute(f'''
                    UPDATE device_connections
                    SET missed_polls = missed_polls + 1
                    WHERE disconnected_at IS NULL
                      AND mac_address NOT IN ({placeholders})
                ''', present_macs)
            else:
                conn.execute('''
                    UPDATE device_connections
                    SET missed_polls = missed_polls + 1
                    WHERE disconnected_at IS NULL
                ''')
            conn.execute('''
                UPDATE device_connections
                SET disconnected_at = CURRENT_TIMESTAMP
                WHERE disconnected_at IS NULL AND missed_polls >= ?
            ''', (DISCONNECT_CONFIRMATION_POLLS,))

            conn.execute('''
                DELETE FROM device_connections
                WHERE disconnected_at IS NOT NULL
                  AND disconnected_at < datetime('now', ?)
            ''', (f'-{CONNECTION_HISTORY_DAYS} days',))
            conn.execute('''
                DELETE FROM device_connections
                WHERE disconnected_at IS NOT NULL
                  AND id NOT IN (
                      SELECT id FROM device_connections
                      WHERE disconnected_at IS NOT NULL
                      ORDER BY disconnected_at DESC, id DESC
                      LIMIT ?
                  )
            ''', (MAX_CLOSED_CONNECTIONS,))
            conn.commit()
            return True
        except Exception as exc:
            conn.rollback()
            self.logger.error("Could not sync connection history: %s", exc)
            return False
        finally:
            conn.close()

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
            new_balance = max(0, current_balance - minutes)

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

    # --- device / plan info ---------------------------------------------------

    def get_device_info(self, mac_address):
        with self._with_conn('Getting device info') as (conn, out):
            row = conn.execute('''
                SELECT time_balance, status, download_limit, upload_limit,
                       plan, upgrade_requested
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

    def create_voucher(self, minutes, price=0):
        """Create a voucher worth the given minutes; returns the code.

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
                        'INSERT INTO vouchers (code, minutes, price) '
                        'VALUES (?, ?, ?)',
                        (code, minutes, price))
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
        """Redeem a voucher for a device; returns minutes granted or None."""
        conn = self._connect()
        try:
            row = conn.execute(
                'SELECT id, minutes FROM vouchers WHERE code = ? AND redeemed_at IS NULL',
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

        if self.add_time(mac_address, 0, row['minutes'], source='voucher'):
            return row['minutes']
        return None

    def get_vouchers(self, include_redeemed=False):
        with self._with_conn('Listing vouchers', default=[]) as (conn, out):
            query = ("SELECT code, minutes, price, "
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

    def get_revenue_summary(self):
        with self._with_conn(
                'Calculating revenue summary',
                on_error=lambda: {'day': 0.0, 'week': 0.0, 'month': 0.0},
        ) as (conn, out):
            row = conn.execute('''
                SELECT
                    COALESCE(SUM(CASE
                        WHEN date(created_at, 'localtime') = date('now', 'localtime')
                        THEN amount ELSE 0
                    END), 0) AS day,
                    COALESCE(SUM(CASE
                        WHEN datetime(created_at, 'localtime')
                             >= datetime('now', 'localtime', '-6 days')
                        THEN amount ELSE 0
                    END), 0) AS week,
                    COALESCE(SUM(CASE
                        WHEN strftime('%Y-%m', created_at, 'localtime')
                             = strftime('%Y-%m', 'now', 'localtime')
                        THEN amount ELSE 0
                    END), 0) AS month
                FROM transactions
                WHERE amount > 0
            ''').fetchone()
            out.result = {
                'day': float(row['day']),
                'week': float(row['week']),
                'month': float(row['month']),
            }
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
