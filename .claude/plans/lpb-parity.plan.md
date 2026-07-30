# Plan: LPB Piso WiFi 2.0 Feature Parity — Admin + Portal

**Reference product**: LPB Piso WiFi 2.0 (https://lpbpisowifi.com/)
**Target**: this repo (Flask + SQLite, single-box Orange Pi / Raspberry Pi gateway)
**Complexity**: Large

## Scope and boundaries

Feature-level parity only. This plan reproduces **capabilities and screen structure**, not LPB's
brand: no LPB name, logo, colour scheme, artwork, copy, or code is used or reproduced. LPB's source
is not available and is not required.

Explicitly **out of scope** for this plan (LPB features that do not apply to a single-box,
self-hosted deployment or that are separate products in their own right):

- Cloud multi-machine session sync and nationwide roaming
- License server / license usage monitoring
- Sub-vendo creation with automatic VLAN selection
- PPPoE subscription server with SMS reminders
- E-loading / cash-in reseller services
- Piso Net (PC rental) and phone rental verticals

## Current baseline

Already implemented in this repo:

| Capability | Location |
|---|---|
| Admin dashboard + live polling | `routes/admin.py:85`, `routes/admin.py:117` |
| Add / deduct / transfer time | `routes/admin.py:199`, `:226`, `:247` |
| Per-device bandwidth limit | `routes/admin.py:280`, `network/qos.py` |
| Rate editor | `routes/admin.py:461`, `templates/rates.html` |
| Voucher generator (incl. duration passes) | `routes/admin.py:497`, `templates/vouchers.html` |
| Transaction log | `templates/transactions.html` |
| Announcement posts / carousel | `routes/admin.py:393`, `templates/portal.html:7` |
| Coin insert flow | `routes/portal.py:144`, `coinslot.py` |
| Voucher redeem | `routes/portal.py:113` |
| Pause / resume | `routes/portal.py:204`, `:227` |
| Game low-latency lane (partial SQM) | `network/firewall.py:154`, `network/qos.py` |

## Gap versus LPB 2.0

| LPB capability | Status here | Step |
|---|---|---|
| System info panel (temp, uptime, disk, services) | missing | 2 |
| Sales report, date filter, CSV export | partial (no filter/export) | 3 |
| Timer schedule (time-of-day / day-of-week rates) | missing | 4 |
| Client & DHCP manager, IP reservation | missing | 5 |
| System tools (reboot, service restart, logs) | missing | 6 |
| Themeable portal (logo, colours, layout) | missing | 7 |
| Data-cap (MB) purchasing alongside time | missing | 8 |
| Portal layout parity | partial | 9 |
| E-payment purchase (GCash / Maya) | missing | 10 |

---

## Step 1 — Feature-parity gap analysis and target architecture

Produce a written gap matrix between LPB 2.0's documented admin and portal surfaces and this
codebase, then propose the target module layout for the new admin sections. Decide which new
concerns become blueprints, which become services in `services.py`, and what the SQLite schema
additions are across all subsequent steps so migrations land once rather than nine times.

Acceptance: gap matrix committed as a markdown table; target blueprint/module layout named with
file paths; a single consolidated schema-change list covering steps 3, 4, 8 and 10.

Out of scope: writing implementation code; the cloud, license, PPPoE, VLAN and rental features
listed in the plan preamble.

## Step 2 — Admin system info and health panel

Build a system health section on the admin dashboard: SoC temperature, CPU load, RAM and disk
usage, uptime, hostapd/dnsmasq/service status, uplink reachability, and connected-client count.
Read from `/proc` and `/sys` directly rather than shelling out where possible; mirror the
degrade-gracefully pattern in `network/firewall.py:154` so a missing sysfs node never 500s the
dashboard.

Acceptance: `/admin` renders a health card with all listed metrics; every metric returns a safe
placeholder instead of raising when its source is unavailable; unit tests cover the missing-source
path for each metric.

Out of scope: historical metric retention or graphing.

## Step 3 — Sales reporting with date filtering and CSV export

Add a sales report view over the existing transactions table: date-range filter, grouping by day
and week and month, breakdown by source (coin, voucher, admin adjustment), and CSV export of the
filtered set. Build on the revenue summary logic already present rather than duplicating it.

Acceptance: date-range filter returns only transactions inside the range in server-local time;
CSV export contains exactly the filtered rows with a header row; totals in the CSV reconcile with
the totals rendered on screen.

Out of scope: PDF export; charting libraries.

## Step 4 — Timer rate schedule

Add scheduled rate overrides so an operator can define time-of-day and day-of-week windows with
different minutes-per-peso than the default (for example a cheaper overnight rate). Resolution
happens at purchase time, and the portal shows the rate currently in force.

Acceptance: a purchase made inside an active window uses the window rate and one outside it uses
the default; overlapping windows resolve deterministically by an explicit documented precedence
rule; the portal displays the currently active rate.

Out of scope: per-device or per-customer rate overrides.

## Step 5 — Client and DHCP manager with IP reservation

Add an admin section listing every DHCP lease with hostname, MAC, IP, vendor, lease expiry, and
current session state; support static IP reservation written into the dnsmasq config, plus
block/unblock. Reservations must survive a config regeneration in `network/ap_manager.py:145`.

Acceptance: reserving an IP writes a `dhcp-host` entry and the client receives that IP after
reconnect; regenerating the AP config preserves all reservations; every MAC and IP that reaches a
shell argument is validated against the existing regex patterns in `config.py` before use.

Out of scope: IPv6 lease management.

## Step 6 — System tools: reboot, service restart, log viewer

Add admin controls to reboot the box, restart hostapd/dnsmasq/the app service, and tail recent
application and system logs in the browser. Every action is a POST behind the existing admin auth
and the local-connection guard at `routes/admin.py:20`, uses a fixed allowlist of commands, and is
written to an audit log with the acting admin and timestamp.

Acceptance: only commands on the hardcoded allowlist can execute and no user input reaches a
command argument; reboot and restart require an explicit confirmation step; every invocation
appends an audit-log entry.

Out of scope: firmware or OS package updates.

## Step 7 — Portal and admin theming

Let the operator set a logo, primary and accent colours, portal layout variant, and custom footer
text from admin settings, rendered through CSS custom properties in `static/app.css`. Reuse the
existing validated image upload helpers at `routes/admin.py:338`.

Acceptance: an uploaded logo and chosen colours render on the portal without an app restart;
uploads are rejected unless they pass the existing format check; a reset control restores the
built-in defaults.

Out of scope: a full drag-and-drop layout builder; per-device theming.

## Step 8 — Data-cap purchasing alongside time

Add data-quota packages so a customer can buy megabytes instead of minutes. Requires per-MAC byte
accounting from iptables counters, a new balance dimension alongside `time_balance`, enforcement
that blocks a device when its quota is exhausted, and portal display of data remaining.

Acceptance: byte counters survive a firewall rule rebuild without resetting a customer's usage to
zero; a device is blocked within one check interval of exhausting its quota; time-based and
data-based balances can coexist on one device with a documented precedence rule.

Out of scope: per-application or per-destination quota accounting.

## Step 9 — Portal layout parity pass

Restructure the customer portal into the card-based layout LPB uses: a status header with a live
countdown, a purchase card offering coin, voucher and e-payment side by side, a session-controls
card for pause and resume, and the announcements carousel below. Keep every existing route and
form contract intact.

Acceptance: all current portal actions (coin, redeem, pause, resume, upgrade request) still work
unchanged; the layout is usable at 360px width; the live countdown updates without a full page
reload.

Out of scope: a native mobile app; changing any portal route or form field name.

## Step 10 — E-payment purchase flow (GCash / Maya)

Integrate a Philippine payment processor supporting GCash and Maya (PayMongo or Xendit) so a
customer can buy time or data without coins. Requires a hosted checkout or QR Ph flow, a webhook
endpoint that credits the device balance, idempotent webhook handling, and a walled-garden
firewall allowance so a zero-balance device can reach the checkout domain.

Acceptance: a completed sandbox payment credits the correct MAC exactly once even when the webhook
is delivered twice; webhook signatures are verified and unsigned or mis-signed requests are
rejected; a device with zero balance can reach the checkout domain and nothing else.

Out of scope: refunds and chargeback handling; cash-in or e-loading reseller features.
