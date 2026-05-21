"""Launcher for the Pedaku web app (Termux / desktop friendly).

Usage:
    python run_web.py                # default 0.0.0.0:5000
    python run_web.py --port 5050    # custom port
    PORT=5050 python run_web.py      # via env var
    python run_web.py --host 127.0.0.1 --port 8080
    python run_web.py --debug        # enable verbose ELM327 / SSE logs
"""
from __future__ import annotations

import argparse
import errno
import logging
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from pedaku.server import create_app
from pedaku.utils.logger import configure as configure_logging


def _port_in_use(host: str, port: int) -> bool:
    """Return True if *host:port* is already bound by another process."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as ex:
            return ex.errno in (errno.EADDRINUSE, errno.EACCES)
    return False


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the Pedaku web app.")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"),
                   help="Bind address (default: 0.0.0.0; env HOST)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")),
                   help="Listen port (default: 5000; env PORT)")
    p.add_argument("--debug", action="store_true",
                   default=os.environ.get("DEBUG", "").lower() in ("1", "true", "yes"),
                   help="Enable DEBUG-level logging (env DEBUG=1)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    host, port = args.host, args.port

    # Wire structured logging early so any TransportError / watchdog message
    # from the diagnostic engine actually shows up in the user's terminal.
    # Default INFO; --debug bumps to DEBUG which traces every ELM327 command.
    configure_logging(level=logging.DEBUG if args.debug else logging.INFO)

    if _port_in_use(host, port):
        sys.stderr.write(
            f"\n  Port {port} sudah dipakai program lain.\n"
            f"    - Cek dengan:  lsof -i :{port}   atau   ss -ltnp | grep :{port}\n"
            f"    - Atau pakai port lain:   python run_web.py --port 5050\n"
            f"    - Atau:                    PORT=5050 python run_web.py\n\n"
        )
        return 1

    app = create_app()
    print(f"\n  Pedaku web running:")
    print(f"    On this device:  http://localhost:{port}")
    print(f"    On the network:  http://<your-ip>:{port}")
    print(f"  Ctrl+C to stop.\n")
    app.run(host=host, port=port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
