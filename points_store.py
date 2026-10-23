"""Loyalty points storage, mixed into UserManager.

Points live in their own MAC-keyed table rather than on `users`:
purge_stale_devices() deletes zero-balance user rows after a day, and a
customer whose time ran out must not lose the points they earned buying it.

points_accounts.points is a cached running balance; points_ledger is the
append-only trail it is derived from (SUM(delta) per MAC == points). Every
write to both happens on the caller's connection so it commits or rolls back
together with whatever earned or spent the points.
"""

# Ledger reasons that represent points bought with money. Only these count
# toward lifetime_earned and the "earned" report total.
EARN_REASONS = ('coin', 'voucher')
REPORT_WINDOW = '-30 days'


class PointsStoreMixin:

    @staticmethod
    def _init_points_schema(cursor):
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS points_accounts (
                mac_address TEXT PRIMARY KEY,
                points REAL NOT NULL DEFAULT 0,
                lifetime_earned REAL NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS points_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mac_address TEXT NOT NULL,
                delta REAL NOT NULL,
                reason TEXT NOT NULL,
                ref TEXT,
                balance_after REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_points_ledger_mac
            ON points_ledger (mac_address, id DESC)
        ''')

    # --- internal writers (caller's connection) ---------------------------

    def _apply_points(self, conn, mac_address, delta, reason, ref=None):
        """Add `delta` (may be negative) to a balance and log it; returns the
        new balance. Callers guarantee the result cannot go below zero."""
        delta = round(float(delta), 4)
        earned = delta if reason in EARN_REASONS and delta > 0 else 0
        conn.execute('''
            INSERT INTO points_accounts (mac_address, points, lifetime_earned)
            VALUES (?, ?, ?)
            ON CONFLICT(mac_address) DO UPDATE SET
                points = ROUND(points + excluded.points, 4),
                lifetime_earned = lifetime_earned + excluded.lifetime_earned,
                updated_at = CURRENT_TIMESTAMP
        ''', (mac_address, delta, earned))
        balance = conn.execute(
            'SELECT points FROM points_accounts WHERE mac_address = ?',
            (mac_address,)).fetchone()['points']
        conn.execute('''
            INSERT INTO points_ledger
                (mac_address, delta, reason, ref, balance_after)
            VALUES (?, ?, ?, ?, ?)
        ''', (mac_address, delta, reason, ref, balance))
        return balance

    def _award_points(self, conn, mac_address, points, reason, ref=None):
        if points and points > 0:
            self._apply_points(conn, mac_address, points, reason, ref)

    def _move_points(self, conn, from_mac, to_mac):
        """Move a whole points balance between MACs; returns points moved."""
        row = conn.execute(
            'SELECT points FROM points_accounts WHERE mac_address = ?',
            (from_mac,)).fetchone()
        if row is None or row['points'] <= 0:
            return None
        moved = row['points']
        self._apply_points(conn, from_mac, -moved, 'transfer_out', to_mac)
        self._apply_points(conn, to_mac, moved, 'transfer_in', from_mac)
        return moved

    def _ensure_user_row(self, conn, mac_address):
        """users.id for a MAC, creating a default-plan row if it was purged."""
        row = conn.execute('SELECT id FROM users WHERE mac_address = ?',
                           (mac_address,)).fetchone()
        if row is not None:
            return row['id']
        plan = self.get_plans().get(
            'default', {'download_kbps': 2048, 'upload_kbps': 1024})
        cursor = conn.execute('''
            INSERT INTO users (mac_address, time_balance, status,
                               download_limit, upload_limit, plan)
            VALUES (?, 0, 'inactive', ?, ?, 'default')
        ''', (mac_address, plan['download_kbps'], plan['upload_kbps']))
        return cursor.lastrowid

    # --- public API -------------------------------------------------------

    def get_points(self, mac_address):
        empty = {'points': 0, 'lifetime_earned': 0}
        with self._with_conn('Reading points',
                             on_error=lambda: dict(empty)) as (conn, out):
            row = conn.execute(
                'SELECT points, lifetime_earned FROM points_accounts '
                'WHERE mac_address = ?', (mac_address,)).fetchone()
            out.result = dict(row) if row else dict(empty)
        return out.result

    def award_points(self, mac_address, points, reason, ref=None):
        """Standalone award, for earners that don't go through add_time."""
        with self._with_conn('Awarding points', default=False) as (conn, out):
            self._award_points(conn, mac_address, points, reason, ref)
            out.result = True
        return out.result

    def redeem_points(self, mac_address, cost, minutes):
        """Spend `cost` points for `minutes` of time, all-or-nothing.

        The balance check and the deduction are one conditional UPDATE, so a
        double-tapped Redeem button can only spend once. A device on a
        duration pass gets its deadline pushed out instead: its time_balance
        is mirrored from expires_at and loose minutes would be overwritten.

        Returns {'minutes', 'points_left', 'expires_at'} or None.
        """
        if cost <= 0 or minutes <= 0:
            return None
        with self._with_conn(f'Redeeming points for {mac_address}') as (conn, out):
            cost = round(float(cost), 4)
            cursor = conn.execute('''
                UPDATE points_accounts
                SET points = ROUND(points - ?, 4), updated_at = CURRENT_TIMESTAMP
                WHERE mac_address = ? AND points >= ?
            ''', (cost, mac_address, cost))
            if cursor.rowcount != 1:
                return
            user_id = self._ensure_user_row(conn, mac_address)
            self._credit_redeemed_time(conn, user_id, minutes)
            tx = conn.execute('''
                INSERT INTO transactions (user_id, amount, minutes, source)
                VALUES (?, 0, ?, 'points')
            ''', (user_id, minutes))
            left = conn.execute(
                'SELECT points FROM points_accounts WHERE mac_address = ?',
                (mac_address,)).fetchone()['points']
            conn.execute('''
                INSERT INTO points_ledger
                    (mac_address, delta, reason, ref, balance_after)
                VALUES (?, ?, 'redeem', ?, ?)
            ''', (mac_address, -cost, str(tx.lastrowid), left))
            expiry = conn.execute(
                "SELECT datetime(expires_at, 'localtime') AS expires_at "
                'FROM users WHERE id = ?', (user_id,)).fetchone()
            out.result = {'minutes': minutes, 'points_left': left,
                          'expires_at': expiry['expires_at']}
        if out.result:
            self.logger.info(
                f"{mac_address} redeemed {cost:g} points for {minutes:g} min")
        return out.result

    @staticmethod
    def _credit_redeemed_time(conn, user_id, minutes):
        row = conn.execute('SELECT expires_at, paused FROM users WHERE id = ?',
                           (user_id,)).fetchone()
        if row['expires_at'] is None:
            conn.execute('''
                UPDATE users SET time_balance = time_balance + ?,
                                 status = 'active'
                WHERE id = ?
            ''', (minutes, user_id))
            return
        # A paused pass resumes later by shifting the deadline by the paused
        # span, so extend from the stored deadline; a live (or lapsed) pass
        # extends from whichever is later, now or the deadline.
        conn.execute('''
            UPDATE users
            SET expires_at = datetime(
                    CASE WHEN paused = 1 THEN expires_at
                         ELSE MAX(expires_at, datetime('now')) END,
                    '+' || ? || ' seconds'),
                time_balance = CASE WHEN paused = 1
                    THEN time_balance + ? ELSE time_balance END,
                status = 'active'
            WHERE id = ?
        ''', (int(float(minutes) * 60), minutes, user_id))
        conn.execute('''
            UPDATE users
            SET time_balance = MAX(0, ROUND(
                    (julianday(expires_at) - julianday('now')) * 1440.0, 2))
            WHERE id = ? AND paused = 0
        ''', (user_id,))

    def adjust_points(self, mac_address, delta, note=''):
        """Admin correction. Clamped so a balance never goes below zero.
        Returns the new balance, or None for a zero adjustment."""
        delta = float(delta)
        if delta == 0:
            return None
        with self._with_conn('Adjusting points') as (conn, out):
            row = conn.execute(
                'SELECT points FROM points_accounts WHERE mac_address = ?',
                (mac_address,)).fetchone()
            current = row['points'] if row else 0
            applied = max(delta, -current)
            out.result = current if applied == 0 else self._apply_points(
                conn, mac_address, applied, 'admin_adjust', note or None)
        return out.result

    def transfer_points(self, from_mac, to_mac):
        """Move points to a device whose randomized MAC rotated. Returns the
        points moved, or None when there was nothing to move."""
        from_mac = (from_mac or '').strip().upper()
        to_mac = (to_mac or '').strip().upper()
        if from_mac == to_mac:
            return None
        with self._with_conn('Transferring points') as (conn, out):
            out.result = self._move_points(conn, from_mac, to_mac)
        return out.result

    def get_points_ledger(self, mac_address=None, limit=200):
        where, params = ('WHERE mac_address = ?', [mac_address]) \
            if mac_address else ('', [])
        with self._with_conn('Listing points ledger', default=[]) as (conn, out):
            rows = conn.execute(f'''
                SELECT id, mac_address, delta, reason, ref, balance_after,
                       datetime(created_at, 'localtime') AS created_at
                FROM points_ledger {where}
                ORDER BY id DESC LIMIT ?
            ''', (*params, limit)).fetchall()
            out.result = [dict(row) for row in rows]
        return out.result

    def get_points_summary(self):
        empty = {'outstanding': 0, 'accounts': 0,
                 'earned_30d': 0, 'redeemed_30d': 0}
        with self._with_conn('Summarizing points',
                             on_error=lambda: dict(empty)) as (conn, out):
            totals = conn.execute('''
                SELECT COALESCE(SUM(points), 0) AS outstanding,
                       COUNT(*) AS accounts
                FROM points_accounts WHERE points > 0
            ''').fetchone()
            placeholders = ','.join('?' for _ in EARN_REASONS)
            flows = conn.execute(f'''
                SELECT
                    COALESCE(SUM(CASE WHEN reason IN ({placeholders})
                                      THEN delta END), 0) AS earned,
                    COALESCE(-SUM(CASE WHEN reason = 'redeem'
                                       THEN delta END), 0) AS redeemed
                FROM points_ledger
                WHERE created_at >= datetime('now', ?)
            ''', (*EARN_REASONS, REPORT_WINDOW)).fetchone()
            out.result = {'outstanding': totals['outstanding'],
                          'accounts': totals['accounts'],
                          'earned_30d': flows['earned'],
                          'redeemed_30d': flows['redeemed']}
        return out.result
