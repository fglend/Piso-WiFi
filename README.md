# PISO WIFI System

A Python-based PISO WIFI management system designed for Orange Pi One that enables pay-per-use WiFi access control. The system allows users to purchase internet time credits and automatically manages their access based on remaining balance.

## Features

- Pay-per-use WiFi access (configurable rate, default 1 peso = 5 minutes via `RATE_MINUTES_PER_PESO`)
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
network/ap_manager.py   hostapd/dnsmasq lifecycle + station discovery
network/firewall.py     iptables access control (PISOWIFI chain)
network/qos.py          tc bandwidth limits
network/command.py      Shell-free subprocess execution
```

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
   docker-compose up -d

   # Or start with logs visible
   docker-compose up
   ```

5. Verify the container is running:
   ```bash
   docker ps | grep piso-wifi
   ```

6. Check the logs:
   ```bash
   docker-compose logs -f
   ```

7. Access the application:
   - Web interface: http://localhost:5000
   - API endpoint: http://localhost:5000/api/v1

8. Stop the container:
   ```bash
   docker-compose down
   ```

Common Docker Commands:
- Rebuild after changes: `docker-compose up --build`
- Remove containers and volumes: `docker-compose down -v`
- View container logs: `docker-compose logs -f`
- Shell access: `docker exec -it piso_wifi bash`
- Check container status: `docker-compose ps`

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
- `AP_SSID`: WiFi network name
- `RATE_MINUTES_PER_PESO`: Minutes granted per peso (default: 5)
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