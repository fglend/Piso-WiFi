# PISO WIFI System

A Python-based PISO WIFI management system designed for Orange Pi One that enables pay-per-use WiFi access control. The system allows users to purchase internet time credits and automatically manages their access based on remaining balance.

## Features

- Pay-per-use WiFi access with an admin-editable price tier table (`/admin/rates`,
  e.g. ₱1 = 10 min … ₱500 = 30 days); amounts decompose greedily across tiers
  (₱6 = ₱5 tier + ₱1 tier). `RATE_MINUTES_PER_PESO` remains as the fallback rate
  for amounts below the smallest tier or when the table is empty
- MAC address-based device tracking and access control (dedicated `PISOWIFI` iptables chain)
- Captive portal for users: view own balance/plan, redeem voucher codes, request premium upgrades
- Web-based admin dashboard (login required) for:
  - Viewing connected devices
  - Managing user time balances and per-device bandwidth
  - Managing plans (stored in the `plans` table) and approving upgrades
  - Generating and tracking voucher codes
  - Monitoring transactions
- Accurate time metering (exact elapsed-time deduction, clock persisted across restarts,
  optional pause-on-disconnect)
- Per-device QoS bandwidth limits via `tc` (HTB + ingress policing)
- Network state reconciliation on startup: paying users keep access after a reboot
- Security hardening: admin auth on all management routes, CSRF protection, MAC/input
  validation, shell-free command execution, production refuses default credentials
- Automatic access blocking when time expires
- Transaction history and reporting
- Containerized development environment
- Production-ready deployment for Orange Pi One

## Architecture

```
main.py                 Flask app factory (create_app)
config.py               Typed settings loaded from .env
services.py             Builds the single shared service instances
auth.py                 Admin auth + CSRF protection
routes/portal.py        User-facing captive portal (/, /redeem, /request_upgrade, /login)
routes/admin.py         Admin dashboard and actions (/admin, /add_time, /vouchers, ...)
user_manager.py         SQLite data layer (users, transactions, plans, vouchers, sessions)
time_manager.py         Background metering thread
network_controller.py   Facade over the network package
network/ap_manager.py   hostapd/dnsmasq lifecycle + station discovery (AP mode)
network/wired.py        Wired gateway mode (external AP/PoE router in bridge mode)
network/firewall.py     iptables access control (PISOWIFI chain)
network/qos.py          tc bandwidth limits
network/command.py      Shell-free subprocess execution
coinslot.py             GPIO pulse coinslot service (CH-926 type)
```

## Wired Gateway Setup (Orange Pi PC + PoE router)

For boards without WiFi (e.g. Orange Pi PC) the Pi acts as a wired gateway:

```
Internet ── USB-to-LAN (eth1, INTERNET_INTERFACE) ── Orange Pi PC ── onboard Ethernet (eth0, LAN_INTERFACE) ── PoE switch/router (AP/bridge mode) ── clients
```

Set `NETWORK_MODE=wired` in `.env`. The Pi runs DHCP, the captive portal,
per-MAC firewall rules and QoS on the LAN interface and NATs out the USB
adapter. **The PoE router must be in Access Point (bridge) mode** — if it
routes/NATs, the Pi only sees the router's MAC and per-device control breaks.
Quick check: a connected phone should get an IP from the Pi's DHCP range
(192.168.4.x by default), not from the router.

## Coinslot

A pulse-type coinslot (CH-926 / Weiyu universal style) wires its SIG/COIN line
to a GPIO pin (`COINSLOT_GPIO`, sysfs number; PA6 = 6 on Orange Pi PC) and GND
to GND. The acceptor's 12V feed runs through a relay (`COINSLOT_RELAY_GPIO`,
sysfs number; PA7 = 7 on Orange Pi PC) instead of straight to the supply, so
the acceptor is only **electrically powered** while a claim is active - not
just software-gated.

Flow: the user taps **Insert Coin** on the portal, which reserves the slot for
their device for `COINSLOT_CLAIM_TIMEOUT` seconds and energizes the relay;
each peso pulse credits `RATE_MINUTES_PER_PESO` minutes to that device
immediately and extends the window. When the window expires (or the service
stops), the relay de-energizes and the acceptor goes dead again. Pulses
arriving with no active claim are additionally ignored in software, so stray
coins during the narrow race at claim expiry still can't credit a random
device. Configure the slot's pulses-per-peso to match
`COINSLOT_PULSES_PER_PESO`.

Wire the relay's **NO (normally-open) + COM** contacts in series with the
acceptor's 12V line - not NC. That way loss of relay/Pi power opens the
contact and the acceptor has no power. A software-only crash can leave a GPIO
output latched until systemd restarts the app, so use a hardware default-OFF
bias (or watchdog relay where required) rather than relying on software alone.
Set `COINSLOT_RELAY_ACTIVE_HIGH=true` only if your relay board energizes on a
HIGH signal; most cheap opto-isolated boards are active-low (the default).

> Note: if the coinslot runs on 12V, power it from the 12V side of your
> step-down converter and route COIN/SIG through a suitable 12V-to-3.3V
> optocoupler/level shifter; never put 5V/12V directly into GPIO. Before
> wiring the relay permanently, verify its control pin stays at the inactive
> level across several power cycles (with `cat
> /sys/class/gpio/gpio7/value`, default active-low must read `1` while off;
> active-high must read `0`) so a boot-time GPIO glitch cannot unexpectedly
> energize it. See `ORANGE_PI_PC_SETUP.md` for the complete wiring procedure.

## System Requirements

### Hardware
- Orange Pi One or similar single board computer
- WiFi adapter supporting AP mode
- Power supply
- Network connectivity

### Software
- Python 3.9+
- Docker and Docker Compose (for development)
- Linux with hostapd and dnsmasq support
- iptables for network access control

## Development Setup

### Prerequisites

- Python 3.9 or higher
- Docker and Docker Compose installed
- Git for version control
- Linux environment (WSL2 for Windows users)

### Quick Start

#### Option 1: Using Docker (Recommended)

1. Clone the repository:
   ```bash
   git clone https://github.com/llTheBlankll/piso-wifi.git
   cd piso-wifi
   ```

2. Build the Docker image:
   ```bash
   # Build with default tag
   docker build -t piso-wifi .

   # Or build with version tag
   docker build -t piso-wifi:1.0 .
   ```

3. Verify the image was created:
   ```bash
   docker images | grep piso-wifi
   ```

4. Start the container using Docker Compose:
   ```bash
   # Start in detached mode
   docker compose up -d

   # Or start with logs visible
   docker compose up
   ```

5. Verify the container is running:
   ```bash
   docker ps | grep piso-wifi
   ```

6. Check the logs:
   ```bash
   docker compose logs -f
   ```

7. Access the application:
   - Web interface: http://localhost:5000
   - API endpoint: http://localhost:5000/api/v1

8. Stop the container:
   ```bash
   docker compose down
   ```

Common Docker Commands:
- Rebuild after changes: `docker compose up --build`
- Remove containers and volumes: `docker compose down -v`
- View container logs: `docker compose logs -f`
- Shell access: `docker exec -it piso_wifi bash`
- Check container status: `docker compose ps`

The default Compose file is for local web-app testing. It sets
`MANAGE_HARDWARE=false`, so the Flask app and SQLite database run without
configuring host WiFi, iptables, dnsmasq, GPIO, or traffic shaping. It keeps
`COINSLOT_ENABLED=true` and a `DEV_FAKE_MAC` so you can see and test the
coin-session UI locally, but physical coin pulses are only read when hardware
management is enabled on the target device. Use `MANAGE_HARDWARE=true` only on
the target Linux hardware.

#### Option 2: Local Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/llTheBlankll/piso-wifi.git
   cd piso-wifi
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure the environment:
   ```bash
   cp .env.example .env
   # Edit .env file with your settings
   ```

5. Initialize the database:
   ```bash
   python manage.py init-db
   ```

6. Start the development server:
   ```bash
   python manage.py runserver
   ```

The admin interface will be available at `http://localhost:8000/admin`

## Production Deployment

### Orange Pi One Setup

1. Flash the latest Armbian OS to your Orange Pi One
2. Install system dependencies:
   ```bash
   sudo apt update
   sudo apt install python3-pip hostapd dnsmasq
   ```

3. Clone and install the application as described in Quick Start
4. Configure the WiFi interface:
   ```bash
   sudo ./scripts/setup_wifi.sh
   ```

5. Enable and start the services:
   ```bash
   sudo systemctl enable pisowifi
   sudo systemctl start pisowifi
   ```

## Configuration

Copy `.env.example` to `.env` and adjust. Key options:

- `WIFI_INTERFACE` / `INTERNET_INTERFACE`: AP and uplink interfaces (default: wlan0 / wlan1)
- `MANAGE_HARDWARE`: Set `false` for local Docker/web-only testing; set `true`
  on Orange Pi hardware where the app should manage WiFi/firewall/QoS.
- `DEV_FAKE_MAC`: Optional local-only MAC fallback for Docker UI testing when
  `MANAGE_HARDWARE=false`.
- `AP_SSID`: WiFi network name
- `RATE_MINUTES_PER_PESO`: Minutes granted per peso (default: 5)
- `PORTAL_TITLE` / `PORTAL_SUBTITLE`: Portal copy defaults; admins can
  override these in the dashboard at runtime.
- `DASHBOARD_REFRESH_SECONDS`: Admin dashboard live refresh interval.
- `DEFAULT_DOWNLOAD_KBPS` / `DEFAULT_UPLOAD_KBPS`: Default plan speed
  fallbacks; admins can override these in the dashboard at runtime.
- `DB_PATH`: SQLite database path (default: `config/piso_wifi.db`)
- `SECRET_KEY`: Flask session key — **required in production**
- `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH`: Admin credentials. Generate the hash with:
  ```bash
  python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yourpassword'))"
  ```
  (`ADMIN_PASSWORD` plaintext works as a fallback; the app refuses to start in
  production with default credentials.)
- `CHECK_INTERVAL`: Seconds between metering checks (default: 5)
- `PAUSE_ON_DISCONNECT`: Freeze balances while a device is offline (default: true)

## Web Interface

- `/` — captive portal: the requesting device sees its own balance, can redeem
  voucher codes and request a premium upgrade
- `/login` — admin login
- `/admin` — dashboard: connected devices, add/deduct time, plans, bandwidth
- `/vouchers` — create and track voucher codes
- `/transactions` — recent top-ups and voucher redemptions

## Running Tests

```bash
python -m pytest tests/ -v
```

The suite covers the data layer, metering logic, firewall/QoS command shapes
(mocked — no root or hardware needed), route authorization, and CSRF.

## Production Notes

Run under gunicorn with a single worker (the AP/firewall state is per-process):

```bash
sudo gunicorn -w 1 -b 0.0.0.0:5000 'main:create_app()'
```

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For support and questions:
- Open an issue on GitHub
- Join our [Discord community](https://discord.gg/pisowifi)
- Email: support@pisowifi.com

## Acknowledgments

- Orange Pi community
- Contributors and testers
- Open source projects used in this system

### Windows-Specific Setup

For Windows users, a special Docker configuration is provided that uses bridge networking instead of WiFi interfaces:

1. Make sure you have Docker Desktop for Windows installed and running

2. Use Windows Terminal or PowerShell to run these commands:
   ```powershell
   # Clone the repository
   git clone https://github.com/llTheBlankll/piso-wifi.git
   cd piso-wifi

   # Start using Windows configuration
   docker-compose -f docker-compose.windows.yml up -d
   ```

3. Verify the setup:
   ```powershell
   # Check container status
   docker ps | findstr piso-wifi

   # Check container logs
   docker-compose -f docker-compose.windows.yml logs -f
   ```

4. Access the application:
   - Web interface: http://localhost:5000
   - API endpoint: http://localhost:5000/api/v1

5. Stop the container:
   ```powershell
   docker-compose -f docker-compose.windows.yml down
   ```

Note: The Windows configuration uses bridge networking instead of direct WiFi interface access. This is suitable for development and testing, but for production deployment, use the Linux configuration on Orange Pi or similar hardware.

Common Issues on Windows:
- If you get permission errors, make sure Docker Desktop has Windows Defender Firewall access
- If the container can't start, try restarting Docker Desktop
- Make sure Hyper-V and WSL2 are properly enabled in Windows features
