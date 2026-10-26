"""Gunicorn settings for the TCC eClearance production web service.

Render sends SIGTERM on deploy; these settings keep the single SQLite-backed
process responsive without exposing it to multi-process write contention.
"""
import os

port = os.environ.get("PORT") or "5000"
bind = f"0.0.0.0:{port}"

# SQLite + one process + threads is the safe configuration here.
worker_class = "gthread"
workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))

timeout = 120
graceful_timeout = 30
keepalive = 5

# Import the app once in the master so init_db() runs exactly once per boot.
preload_app = True

# Behind Render's proxy.
forwarded_allow_ips = "*"

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
