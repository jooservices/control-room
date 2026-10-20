"""Read-only web server: ``python3 -m controlroom.server``.

Serves the static page from ``web/``, ``GET /api/snapshot`` (the file written
by the collector), ``GET /api/history?metric=&since=`` (series from the SQLite
store), ``GET /api/stream`` (server-sent events) and ``GET /healthz``. Only GET
and HEAD are accepted and no endpoint writes anything.
"""

import argparse
import json
import socket
import sqlite3
import sys
import time
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from controlroom.collector import HISTORY_FILE, SNAPSHOT_FILE
from controlroom.config import ConfigurationError, load_config
from controlroom.history import RETENTION_DAYS, HistoryStore, is_known_metric

STALE_INTERVALS = 3
KEEPALIVE_SECONDS = 20.0
STREAM_POLL_SECONDS = 0.5
MAX_HISTORY_SECONDS = RETENTION_DAYS * 86400
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
                 history_path: Path | None = None,
                 keepalive_seconds: float = KEEPALIVE_SECONDS,
                 poll_seconds: float = STREAM_POLL_SECONDS,
                 **kwargs) -> None:
        self.web_dir = web_dir
        self.snapshot_path = snapshot_path
        self.history_path = history_path
        self.keepalive_seconds = keepalive_seconds
        self.poll_seconds = poll_seconds
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
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/api/snapshot":
            self._snapshot(send_body)
        elif path == "/api/history":
            self._history(parts.query, send_body)
        elif path == "/api/stream":
            self._stream(send_body)
        elif path == "/healthz":
            query = parse_qs(urlsplit(self.path).query)
            self._healthz(send_body, detail=query.get("detail") == ["1"])
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

    def _history(self, query: str, send_body: bool) -> None:
        params = parse_qs(query)
        metric = (params.get("metric") or [""])[0]
        if not is_known_metric(metric):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "unknown metric"}, send_body)
            return
        now = int(self.clock())
        floor = now - MAX_HISTORY_SECONDS
        raw_since = (params.get("since") or [None])[0]
        if raw_since is None:
            since = floor
        else:
            try:
                since = int(raw_since)
                if not -2**63 <= since < 2**63:
                    raise ValueError(raw_since)
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST,
                           {"error": "since must be unix seconds"}, send_body)
                return
            since = max(since, floor)
        try:
            points = HistoryStore(self.history_path, read_only=True).query(metric, since)
        except (sqlite3.Error, TypeError):
            self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                       {"error": "history unavailable"}, send_body)
            return
        self._json(HTTPStatus.OK, {"metric": metric, "since": since,
                                   "points": [list(point) for point in points]},
                   send_body)

    def _stream_event(self) -> bytes | None:
        try:
            body = self.snapshot_path.read_bytes()
        except OSError:
            return None
        return b"event: snapshot\ndata: " + body.replace(b"\n", b"") + b"\n\n"

    def _snapshot_version(self) -> tuple[int, int] | None:
        try:
            info = self.snapshot_path.stat()
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size

    def _stream(self, send_body: bool) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True
        if not send_body:
            return
        seen = None
        last_write = time.monotonic()
        try:
            while True:
                version = self._snapshot_version()
                if version is not None and version != seen:
                    event = self._stream_event()
                    if event is not None:
                        seen = version
                        self.wfile.write(event)
                        self.wfile.flush()
                        last_write = time.monotonic()
                elif time.monotonic() - last_write >= self.keepalive_seconds:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                time.sleep(self.poll_seconds)
        except (OSError, ValueError):
            return  # the client went away

    def _healthz(self, send_body: bool, detail: bool = False) -> None:
        try:
            age = max(0.0, self.clock() - self.snapshot_path.stat().st_mtime)
        except OSError:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE,
                       {"ok": False, "ageSeconds": None}, send_body)
            return
        healthy = age < self.max_age
        report: dict[str, object] = {"ok": healthy, "ageSeconds": round(age, 1)}
        if detail:
            report.update(self._collector_detail())
        self._json(HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
                   report, send_body)

    def _collector_detail(self) -> dict[str, object]:
        """Collector state from the snapshot's ``meta`` (empty when unreadable)."""
        try:
            meta = json.loads(self.snapshot_path.read_text(encoding="utf-8"))["meta"]
        except (OSError, ValueError, KeyError, TypeError):
            meta = None
        if not isinstance(meta, dict):
            meta = {}
        return {
            "asOf": meta.get("asOf"),
            "collector": meta.get("collector"),
            "sources": meta.get("ok") or {},
            "errorAt": meta.get("errorAt") or {},
        }

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
                interval_seconds: int, history_path: Path | None = None,
                clock: Callable[[], float] = time.time,
                keepalive_seconds: float = KEEPALIVE_SECONDS,
                poll_seconds: float = STREAM_POLL_SECONDS) -> ThreadingHTTPServer:
    """Build the server; the caller runs ``serve_forever``."""
    handler = partial(Handler, web_dir=Path(web_dir), snapshot_path=Path(snapshot_path),
                      max_age=STALE_INTERVALS * interval_seconds, clock=clock,
                      history_path=Path(history_path) if history_path else None,
                      keepalive_seconds=keepalive_seconds, poll_seconds=poll_seconds)
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
                         history_path=config.data_dir / HISTORY_FILE,
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
