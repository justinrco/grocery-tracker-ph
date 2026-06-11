"""Run the grocery tracker on the local network (single-process, no auto-reload).

This serves the app with waitress as one foreground process bound to the
terminal, so it shuts down cleanly when you press Ctrl+C *or* simply close the
terminal window — there is no separate reloader worker that could be left
orphaned holding the port.

Trade-off: it does NOT auto-reload. After changing code or templates, stop it
and start it again to pick up the changes.

It binds to all interfaces so other devices on the same Wi-Fi/LAN can reach it.

Usage (start):   .venv\\Scripts\\python.exe serve.py
Stop:            press Ctrl+C, or just close the terminal
Override port:   set PORT=8000 && .venv\\Scripts\\python.exe serve.py
"""
import os
import socket

from waitress import serve

from app import app


def _lan_ip():
    """Best-effort detection of this machine's primary LAN IPv4 address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no traffic sent; just picks the route's source IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    print(f"Grocery tracker on http://{host}:{port}")
    print(f"  This machine:                http://127.0.0.1:{port}")
    print(f"  Other devices on this Wi-Fi: http://{_lan_ip()}:{port}")
    print("  Stop with Ctrl+C, or just close this terminal.")
    serve(app, host=host, port=port)
