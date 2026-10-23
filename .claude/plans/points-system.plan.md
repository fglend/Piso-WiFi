# Plan: Loyalty Points System (per-MAC)

**Target**: this repo (Flask + SQLite, single-box gateway)
**Complexity**: Medium

## Decisions (confirmed with owner)

| Question | Decision |
|---|---|
| Earn basis | **Admin chooses**: per peso paid *or* per minute purchased (runtime setting) |
| Earning sources | **Coinslot + paid vouchers** (voucher `price > 0`). Admin-added time, free vouchers, and time bought with points never earn |
| Redemption | **Convert to free time**: admin sets "N points = M minutes" |
| Expiry | **Never** — points stay on the MAC until redeemed |
| Identity | MAC address (same identity the portal already uses via `_client_mac()`) |

## Key design constraints found in the code

1. **Points must not live on `users`.** `UserManager.purge_stale_devices` (`user_manager.py:1083`)
   deletes zero-balance user rows after 24h. A customer whose time runs out would lose their
   points. → Separate `points_accounts` table keyed on MAC that the purge never touches.
2. **Earning must be atomic with crediting.** `add_time` (`user_manager.py:789`) inserts the
   `transactions` row in one DB transaction; points are written in that same transaction so a
   crash can never credit time without points (or vice versa).
3. **Coinslot credits per coin, not per session.** `_flush_credit_if_settled` (`coinslot.py:230`)
   already computes the peso delta *and* the tiered minute delta for each settled coin — both
   inputs either earn mode needs.
4. **Redemption must be double-spend safe.** A double-tap on "Redeem" sends two POSTs; the
   balance check and deduction must be one conditional `UPDATE ... WHERE points >= ?`.
5. **MAC rotation.** `transfer_balance` (`user_manager.py:1017`) exists because phones rotate
   randomized MACs; points must move with it.

## Data model (additive migrations in `_init_db`)

```sql
CREATE TABLE IF NOT EXISTS points_accounts (
    mac_address     TEXT PRIMARY KEY,
    points          REAL NOT NULL DEFAULT 0,   -- current spendable balance
    lifetime_earned REAL NOT NULL DEFAULT 0,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Append-only audit trail; points_accounts.points == SUM(delta) per MAC.
CREATE TABLE IF NOT EXISTS points_ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    mac_address    TEXT NOT NULL,
    delta          REAL NOT NULL,              -- +earn / -redeem / +-adjust
    reason         TEXT NOT NULL,              -- coin | voucher | redeem | admin_adjust | transfer_in | transfer_out
    ref            TEXT,                       -- transaction id / voucher code
    balance_after  REAL NOT NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_points_ledger_mac ON points_ledger (mac_address, created_at DESC);
```

Points are stored as REAL so small coins in per-minute mode don't lose fractions; the UI shows
`floor(points)` and redemption spends whole points only.

## Runtime settings (`services.app_setting_defaults` / `refresh_runtime_settings`, `config.py`)

| Key | Default | Meaning |
|---|---|---|
| `points_enabled` | `0` | Master switch (ship dark; owner turns on) |
| `points_earn_mode` | `peso` | `peso` or `minute` |
| `points_per_peso` | `1` | Used when mode = `peso` |
| `points_per_minute` | `0.1` | Used when mode = `minute` (0.1 = 1 pt per 10 min) |
| `points_redeem_cost` | `100` | Points per redemption unit |
| `points_redeem_minutes` | `60` | Minutes granted per unit |

Validated on save (non-negative numbers, mode in allow-list, cost > 0), same as existing
settings in `routes/admin.py:161`.

---

## Phase 1 — Pure earn calculation (`loyalty.py`, new, ~40 lines)

Mirror `pricing.py`: a side-effect-free module.

- `compute_points(pesos, minutes, mode, per_peso, per_minute) -> float`
- Returns 0 for unknown mode / disabled / negative input.

**Tests first** `tests/test_loyalty.py`: peso mode, minute mode, zero/negative, unknown mode,
tiered example (₱5 coin = 60 min → 5 pts peso-mode vs 6 pts minute-mode).

## Phase 2 — Storage layer (`user_manager.py`)

- Schema above in `_init_db`.
- `_award_points(conn, mac, delta, reason, ref)` — internal helper that upserts
  `points_accounts` and appends `points_ledger` **using the caller's connection**.
- `add_time(..., points=0)` — new optional arg; when `> 0`, calls `_award_points` inside the
  existing transaction. Default 0 keeps every current caller unchanged.
- `get_points(mac) -> {'points', 'lifetime_earned'}`
- `redeem_points(mac, cost, minutes) -> minutes | None` — single transaction:
  `UPDATE points_accounts SET points = points - ? WHERE mac_address = ? AND points >= ?`;
  if rowcount == 1, ledger row + credit time with `source='points'` (amount 0, so revenue
  reports are unaffected).
- `adjust_points(mac, delta, note)` for admin corrections (never below 0).
- `get_points_ledger(mac=None, limit=200)`.
- `transfer_balance` — also move `points_accounts` balance with `transfer_out/transfer_in`
  ledger rows, in the same transaction.

**Tests first** in `tests/test_user_manager.py`: award is atomic with add_time (force a failure
→ neither written); purge leaves points intact; concurrent/double redeem only succeeds once;
insufficient points → None and nothing changes; transfer moves points; ledger sum == balance.

## Phase 3 — Earn hooks

- **Coinslot** (`coinslot.py:_flush_credit_if_settled`): snapshot the points settings into the
  claim at `claim()` time (like the existing `'rates'` snapshot, so a mid-session settings change
  can't split one session across two rules), compute points from `delta` pesos and `minutes`,
  pass `points=` to `add_time`.
- **Paid vouchers** (`user_manager.redeem_voucher`): when `price > 0`, award points using
  `price` as pesos and the voucher's minutes (or `duration_days * 1440` for passes) as minutes.
  Duration-pass vouchers go through `grant_duration`, so award there via `_award_points` on the
  same connection. Free vouchers (`price = 0`) earn nothing.
- Settings are passed in by the caller (coinslot / route), keeping `UserManager` free of config.

**Tests first**: `tests/test_coinslot.py` — a ₱5 coin earns per mode; disabled → 0; settings
changed mid-claim don't apply until next claim. Voucher: paid earns, free doesn't, pass earns.

## Phase 4 — Portal (customer)

- `routes/portal.py`:
  - `index()` passes `points`, `points_enabled`, redeem cost/minutes to the template.
  - `POST /points/redeem` — MAC from `_client_mac()` only (never from form data), calls
    `redeem_points`, then `unblock_mac` + `set_bandwidth_limit` exactly like `redeem()`.
    Flash success / "not enough points". CSRF is already enforced app-wide via `init_csrf`.
  - `/coin_status` JSON gains `points` so the live coin sheet updates as coins drop.
- `templates/portal.html`: points card ("You have 37 points · 100 pts = 1 hour") with Redeem
  button disabled below cost; hint that turning off "Private Wi-Fi address" keeps points safe.

**Open item to verify during implementation**: redeeming while a duration pass is active —
check how `add_time` interacts with `sync_expiring_devices`; if minutes would be overwritten,
either extend `expires_at` or block redemption during a pass with a clear message.

**Tests first** `tests/test_routes.py`: redeem success credits time + unblocks; insufficient
points; disabled feature → 404/flash; MAC unresolved → error; double POST spends once.

## Phase 5 — Admin

- Settings page (`templates/settings.html`, `routes/admin.py:update_settings`): "Loyalty points"
  section with the six settings and validation.
- Device row / detail: show points; small "Adjust points" form → `adjust_points` + `log_audit`.
- `GET /admin/points` — ledger view (filter by MAC), plus totals: points outstanding (liability
  in minutes), earned and redeemed this month.
- Audit-log every admin adjustment and settings change (existing `log_audit`).

**Tests first**: settings validation rejects bad mode / negative values; adjust writes ledger +
audit; non-admin gets redirected.

## Phase 6 — Review & ship

- `code-reviewer` + `security-reviewer` passes (focus: redemption race, MAC-from-IP only,
  settings input validation, no revenue double-count).
- Coverage ≥ 80% on new code; full suite green.
- Ship with `points_enabled = 0`; enable on the device from the settings page.
- Update `README.md` feature list and the backup notes in `ORANGE_PI_PC_SETUP.md`
  (new tables live in the same SQLite file, so no new backup paths).

## Files touched

| File | Change |
|---|---|
| `loyalty.py` | **new** — pure earn calculation |
| `user_manager.py` | schema, `_award_points`, `add_time(points=)`, redeem/adjust/get/ledger, transfer, voucher earn |
| `coinslot.py` | snapshot points settings in claim, pass points on credit |
| `config.py`, `services.py` | six runtime settings |
| `routes/portal.py` | points in index + coin_status, `POST /points/redeem` |
| `routes/admin.py` | settings section, adjust endpoint, `/admin/points` ledger |
| `templates/portal.html`, `settings.html`, new `points.html` | UI |
| `tests/test_loyalty.py` (new), `test_user_manager.py`, `test_coinslot.py`, `test_routes.py` | tests |

## Risks

| Risk | Mitigation |
|---|---|
| Randomized/rotating MACs lose points | Portal hint + existing admin transfer moves points |
| MAC spoofing steals points | Same exposure as time balances today; points redeem only to the spoofed MAC's own time, audit trail in ledger |
| Mode switch changes earning mid-day | Coinslot snapshots settings per claim; ledger records actual awards |
| Points-bought time inflating revenue | Redeem credits with `amount = 0`, `source = 'points'`; excluded from earning |
