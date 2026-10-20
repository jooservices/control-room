"""Read-only web server: ``python3 -m controlroom.server``.

Serves the static page from ``web/``, ``GET /api/snapshot`` (the file written
by the collector) and ``GET /healthz``. Only GET and HEAD are accepted and no
endpoint writes anything.
"""

import argparse
import json
import socket
import sys
import time
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from controlroom.collector import SNAPSHOT_FILE
from controlroom.config import ConfigurationError, load_config

STALE_INTERVALS = 3
DEFAULT_WEB_DIR = Path(__file__).resolve().parent.parent / "web"
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}


def lan_address() -> str:
    """Return this machine's LAN address, or loopback when it has none.

    Connecting a UDP socket sends no packet; it only selects the outgoing route.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "controlroom"
    sys_version = ""

    def __init__(self, *args, web_dir: Path, snapshot_path: Path,
                 max_age: float, clock: Callable[[], float] = time.time,
                 **kwargs) -> None:
        self.web_dir = web_dir
        self.snapshot_path = snapshot_path
        self.max_age = max_age
        self.clock = clock
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        self._dispatch(send_body=True)

    def do_HEAD(self) -> None:
        self._dispatch(send_body=False)

    def _reject_method(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"method not allowed\n",
                   "text/plain; charset=utf-8", True, {"Allow": "GET, HEAD"}, api=True)

    def __getattr__(self, name: str):
        # Any other verb (POST, TRACE, CONNECT, custom ones) is answered with 405.
        if name.startswith("do_"):
            return self._reject_method
        raise AttributeError(name)

    def _dispatch(self, send_body: bool) -> None:
        path = urlsplit(self.path).path
        if path == "/api/snapshot":
            self._snapshot(send_body)
        elif path == "/healthz":
            self._healthz(send_body)
        else:
            self._static(path, send_body)

    def _snapshot(self, send_body: bool) -> None:
        try:
            body = self.snapshot_path.read_bytes()
        except OSError:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                       {"error": "snapshot unavailable"}, send_body)
            return
        self._send(HTTPStatus.OK, body, "application/json", send_body, api=True)

    def _healthz(self, send_body: bool) -> None:
        try:
            age = max(0.0, self.clock() - self.snapshot_path.stat().st_mtime)
        except OSError:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                       {"ok": False, "ageSeconds": None}, send_body)
            return
        healthy = age < self.max_age
        self._json(HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                   {"ok": healthy, "ageSeconds": round(age, 1)}, send_body)

    def _static(self, path: str, send_body: bool) -> None:
        relative = unquote(path).lstrip("/") or "index.html"
        root = self.web_dir.resolve()
        try:
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise FileNotFoundError(relative)
            body = target.read_bytes()
        except (OSError, ValueError):
            self._send(HTTPStatus.NOT_FOUND, b"not found\n",
                       "text/plain; charset=utf-8", send_body)
            return
        content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._send(HTTPStatus.OK, body, content_type, send_body)

    def _json(self, status: HTTPStatus, value: object, send_body: bool) -> None:
        body = json.dumps(value).encode("utf-8")
        self._send(status, body, "application/json", send_body, api=True)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str,
              send_body: bool, extra: dict[str, str] | None = None,
              api: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        if api:
            self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.address_string(), format % args))


def make_server(host: str, port: int, *, web_dir: Path, snapshot_path: Path,
                interval_seconds: int,
                clock: Callable[[], float] = time.time) -> ThreadingHTTPServer:
    """Build the server; the caller runs ``serve_forever``."""
    handler = partial(Handler, web_dir=Path(web_dir), snapshot_path=Path(snapshot_path),
                      max_age=STALE_INTERVALS * interval_seconds, clock=clock)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="controlroom.server")
    parser.add_argument("--env-file", default=".env", help="configuration file")
    parser.add_argument("--web-dir", default=str(DEFAULT_WEB_DIR),
                        help="directory with the static page")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.env_file)
    except ConfigurationError as error:
        print("Configuration error: {}".format(error), file=sys.stderr)
        return 2

    host = config.bind_host or lan_address()
    server = make_server(host, config.port, web_dir=Path(args.web_dir),
                         snapshot_path=config.data_dir / SNAPSHOT_FILE,
                         interval_seconds=config.interval_seconds)
    print("serving on http://{}:{}".format(host, server.server_address[1]), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
