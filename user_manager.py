import logging
import os
import secrets
import sqlite3
import string


class UserManager:
    def __init__(self, db_path='config/piso_wifi.db'):
        self.db_path = db_path
        self.logger = logging.getLogger(__name__)

        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

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

            # Additive column migrations for databases created by older versions
            self._add_column_if_missing(c, 'transactions', 'source', "TEXT DEFAULT 'cash'")

            # Seed plans
            c.execute('''INSERT OR IGNORE INTO plans (name, download_kbps, upload_kbps)
                         VALUES ('default', 2048, 1024), ('premium', 8096, 8096)''')

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

    # --- dynamic app settings ------------------------------------------------

    def get_app_settings(self, defaults):
        conn = self._connect()
        try:
            rows = conn.execute('SELECT key, value FROM app_settings').fetchall()
            stored = {row['key']: row['value'] for row in rows}
            return {**defaults, **stored}
        except Exception as e:
            self.logger.error(f"Error loading app settings: {e}")
            return dict(defaults)
        finally:
            conn.close()

    def update_app_settings(self, values):
        conn = self._connect()
        try:
            for key, value in values.items():
                conn.execute('''
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                ''', (key, str(value)))
            conn.commit()
            return True
        except Exception as e:
            self.logger.error(f"Error saving app settings: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

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
        conn = self._connect()
        try:
            row = conn.execute('SELECT time_balance FROM users WHERE mac_address = ?',
                               (mac_address,)).fetchone()
            return row['time_balance'] if row else 0
        except Exception as e:
            self.logger.error(f"Error checking balance: {e}")
            return 0
        finally:
            conn.close()

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
        conn = self._connect()
        try:
            row = conn.execute('''
                SELECT time_balance, status, download_limit, upload_limit,
                       plan, upgrade_requested
                FROM users WHERE mac_address = ?
            ''', (mac_address,)).fetchone()
            return dict(row) if row else None
        except Exception as e:
            self.logger.error(f"Error getting device info: {e}")
            return None
        finally:
            conn.close()

    def get_active_users(self):
        """Users with remaining balance - used to reconcile network rules."""
        conn = self._connect()
        try:
            rows = conn.execute('''
                SELECT mac_address, download_limit, upload_limit
                FROM users WHERE time_balance > 0
            ''').fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            self.logger.error(f"Error listing active users: {e}")
            return []
        finally:
            conn.close()

    def request_upgrade(self, mac_address):
        conn = self._connect()
        try:
            conn.execute('UPDATE users SET upgrade_requested = 1 WHERE mac_address = ?',
                         (mac_address,))
            conn.commit()
            return True
        except Exception as e:
            self.logger.error(f"Error requesting upgrade: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def get_plans(self):
        conn = self._connect()
        try:
            rows = conn.execute('SELECT name, download_kbps, upload_kbps FROM plans').fetchall()
            return {r['name']: dict(r) for r in rows}
        except Exception as e:
            self.logger.error(f"Error listing plans: {e}")
            return {}
        finally:
            conn.close()

    def upsert_plan(self, name, download_kbps, upload_kbps):
        conn = self._connect()
        try:
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
            conn.commit()
            return True
        except Exception as e:
            self.logger.error(f"Error saving plan {name}: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def set_plan(self, mac_address, plan_name):
        """Assign a plan; returns (download_kbps, upload_kbps) or None."""
        plan = self.get_plans().get(plan_name)
        if not plan:
            self.logger.error(f"Unknown plan: {plan_name}")
            return None
        conn = self._connect()
        try:
            conn.execute('''
                UPDATE users
                SET plan = ?, download_limit = ?, upload_limit = ?, upgrade_requested = 0
                WHERE mac_address = ?
            ''', (plan_name, plan['download_kbps'], plan['upload_kbps'], mac_address))
            conn.commit()
            return plan['download_kbps'], plan['upload_kbps']
        except Exception as e:
            self.logger.error(f"Error setting plan: {e}")
            conn.rollback()
            return None
        finally:
            conn.close()

    def set_bandwidth(self, mac_address, download_kbps, upload_kbps):
        conn = self._connect()
        try:
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
            conn.commit()
            return True
        except Exception as e:
            self.logger.error(f"Error setting bandwidth: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    # --- vouchers -------------------------------------------------------------

    def create_voucher(self, minutes):
        """Create a voucher worth the given minutes; returns the code."""
        alphabet = string.ascii_uppercase + string.digits
        conn = self._connect()
        try:
            for _ in range(10):
                code = '-'.join(
                    ''.join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))
                try:
                    conn.execute('INSERT INTO vouchers (code, minutes) VALUES (?, ?)',
                                 (code, minutes))
                    conn.commit()
                    self.logger.info(f"Created voucher {code} worth {minutes} minutes")
                    return code
                except sqlite3.IntegrityError:
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
        conn = self._connect()
        try:
            query = 'SELECT code, minutes, created_at, redeemed_by, redeemed_at FROM vouchers'
            if not include_redeemed:
                query += ' WHERE redeemed_at IS NULL'
            query += ' ORDER BY created_at DESC'
            return [dict(r) for r in conn.execute(query).fetchall()]
        except Exception as e:
            self.logger.error(f"Error listing vouchers: {e}")
            return []
        finally:
            conn.close()

    # --- transactions -----------------------------------------------------------

    def get_transactions(self, limit=50):
        conn = self._connect()
        try:
            rows = conn.execute('''
                SELECT t.amount, t.minutes, t.source, t.created_at, u.mac_address
                FROM transactions t LEFT JOIN users u ON u.id = t.user_id
                ORDER BY t.created_at DESC LIMIT ?
            ''', (limit,)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            self.logger.error(f"Error listing transactions: {e}")
            return []
        finally:
            conn.close()

    def get_revenue_summary(self):
        conn = self._connect()
        try:
            row = conn.execute('''
                SELECT
                    COALESCE(SUM(CASE
                        WHEN date(created_at) = date('now', 'localtime') THEN amount
                        ELSE 0
                    END), 0) AS day,
                    COALESCE(SUM(CASE
                        WHEN datetime(created_at) >= datetime('now', 'localtime', '-6 days')
                        THEN amount ELSE 0
                    END), 0) AS week,
                    COALESCE(SUM(CASE
                        WHEN strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now', 'localtime')
                        THEN amount ELSE 0
                    END), 0) AS month
                FROM transactions
                WHERE amount > 0
            ''').fetchone()
            return {
                'day': float(row['day']),
                'week': float(row['week']),
                'month': float(row['month']),
            }
        except Exception as e:
            self.logger.error(f"Error calculating revenue summary: {e}")
            return {'day': 0.0, 'week': 0.0, 'month': 0.0}
        finally:
            conn.close()

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
