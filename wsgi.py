"""Gunicorn entry point.

MUST run with exactly ONE worker (--workers 1): create_app() constructs the
AP, firewall, QoS and time-manager singletons in-process; a second worker
would reconfigure the access point and double-meter customers. Use threads
for concurrency instead (--threads N).
"""
from config import load_settings
from main import create_app

settings = load_settings()
app = create_app(
    start_time_manager=settings.manage_hardware,
    manage_hardware=settings.manage_hardware,
    settings=settings,
)
