import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from controlroom.history import HistoryStore
from controlroom.server import CONTENT_SECURITY_POLICY, lan_address, make_server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.web = root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<h1>page</h1>", encoding="utf-8")
        (root / "secret.txt").write_text("secret", encoding="utf-8")
        self.snapshot = root / "snapshot.json"
        self.snapshot.write_text('{"version":1}', encoding="utf-8")
        self.history = root / "history.db"
        self.now = int(time.time())
        store = HistoryStore(self.history)
        store.insert(self.now - 100 * 86400, {"machine.load": 9.0})
        store.insert(self.now - 3600, {"machine.load": 1.5, "ai.codex.minutes24h": 12.0})
        store.insert(self.now - 60, {"machine.load": 2.5})
        self.server = make_server(
            "127.0.0.1", 0, web_dir=self.web, snapshot_path=self.snapshot,
            interval_seconds=10, history_path=self.history,
            keepalive_seconds=0.3, poll_seconds=0.05,
        )
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response, response.read()
        finally:
            connection.close()

    def open_stream(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/stream")
        return connection.getresponse()

    @staticmethod
    def next_data(response):
        while True:
            line = response.fp.readline().decode("utf-8")
            if line.startswith("data:"):
                return line.strip()

    def test_stream_sends_the_snapshot_as_an_event(self):
        response = self.open_stream()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"),
                         "text/event-stream; charset=utf-8")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.fp.readline(), b"event: snapshot\n")
        self.assertEqual(self.next_data(response), 'data: {"version": 1}')

    def test_stream_pushes_again_when_the_snapshot_changes(self):
        response = self.open_stream()
        self.next_data(response)
        self.snapshot.write_text('{"version":2}', encoding="utf-8")
        os.utime(self.snapshot, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.next_data(response), 'data: {"version": 2}')

    def test_static_page_and_security_headers(self):
        status, response, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<h1>page</h1>")
        self.assertEqual(response.getheader("Content-Security-Policy"),
                         CONTENT_SECURITY_POLICY)

    def test_snapshot_is_served_without_caching(self):
        status, response, body = self.request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"version":1}')
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertIn("default-src 'none'",
                      response.getheader("Content-Security-Policy"))

    def test_missing_snapshot_is_503(self):
        self.snapshot.unlink()
        self.assertEqual(self.request("GET", "/api/snapshot")[0], 503)
        self.assertEqual(self.request("GET", "/healthz")[0], 503)

    def test_head_has_no_body(self):
        status, response, body = self.request("HEAD", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(response.getheader("Content-Length"), "13")

    def test_other_methods_return_405_and_write_nothing(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT", "PROPFIND"):
            status, response, _ = self.request(method, "/api/snapshot")
            self.assertEqual(status, 405, method)
            self.assertEqual(response.getheader("Allow"), "GET, HEAD")
            self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(self.snapshot.read_text(encoding="utf-8"), '{"version":1}')

    def test_healthz_fresh_then_stale(self):
        status, response, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        old = time.time() - 31
        os.utime(self.snapshot, (old, old))
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 503)
        self.assertIn(b'"ageSeconds": 31', body)

    def test_healthz_detail_returns_collector_meta(self):
        meta = {"asOf": "2026-10-20T12:00:00Z", "ok": {"jira": False},
                "errorAt": {"jira": "2026-10-20T12:00:00Z"},
                "collector": {"version": "abc1234", "loopCount": 7,
                              "lastLoopSeconds": 1.5, "sourceErrors": {"jira": 2}}}
        self.snapshot.write_text(json.dumps({"meta": meta}), encoding="utf-8")
        status, response, body = self.request("GET", "/healthz?detail=1")
        report = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertTrue(report["ok"])
        self.assertEqual(report["collector"], meta["collector"])
        self.assertEqual(report["sources"], {"jira": False})
        self.assertEqual(report["asOf"], meta["asOf"])
        self.assertNotIn("collector", json.loads(self.request("GET", "/healthz")[2]))

    def test_healthz_detail_tolerates_snapshot_without_meta(self):
        status, _, body = self.request("GET", "/healthz?detail=1")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["collector"])

    def test_path_traversal_returns_404(self):
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt",
                     "/missing.html"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def points(self, query):
        status, _, body = self.request("GET", "/api/history" + query)
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_history_returns_series_capped_at_90_days(self):
        data = self.points("?metric=machine.load&since=0")
        self.assertEqual([point[1] for point in data["points"]], [1.5, 2.5])
        self.assertGreaterEqual(data["since"], self.now - 90 * 86400)
        _, response, _ = self.request("GET", "/api/history?metric=machine.load")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_history_since_filters_and_ai_minutes(self):
        data = self.points("?metric=machine.load&since=%d" % (self.now - 120))
        self.assertEqual([point[1] for point in data["points"]], [2.5])
        data = self.points("?metric=ai.codex.minutes24h")
        self.assertEqual([point[1] for point in data["points"]], [12.0])

    def test_history_rejects_bad_input_with_400(self):
        for query in ("", "?metric=nope", "?metric=machine.load%3B--",
                      "?metric=ai.x.runs24h", "?metric=machine.load&since=abc",
                      "?metric=machine.load&since=9223372036854775808",
                      "?metric=machine.load&since=-9223372036854775809"):
            self.assertEqual(self.request("GET", "/api/history" + query)[0], 400, query)

    def test_history_without_store_is_503(self):
        self.history.unlink()
        self.assertEqual(self.request("GET", "/api/history?metric=machine.load")[0], 503)

    def read_event(self, response):
        lines = []
        while True:
            line = response.fp.readline().decode("utf-8")
            if line == "\n":
                if lines:
                    return lines
                continue
            lines.append(line.rstrip("\n"))

    def test_stream_sends_event_after_snapshot_changes_and_keepalive(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/stream")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"),
                         "text/event-stream; charset=utf-8")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(self.read_event(response),
                         ["event: snapshot", 'data: {"version":1}'])
        self.assertEqual(self.read_event(response), [": keep-alive"])
        self.snapshot.write_text('{"version":2}', encoding="utf-8")
        later = time.time() + 5
        os.utime(self.snapshot, (later, later))
        while True:
            lines = self.read_event(response)
            if lines != [": keep-alive"]:
                break
        self.assertEqual(lines, ["event: snapshot", 'data: {"version":2}'])

    def test_lan_address_is_not_wildcard(self):
        self.assertNotEqual(lan_address(), "0.0.0.0")


class FooterAssetTests(unittest.TestCase):
    WEB = Path(__file__).resolve().parent.parent / "web"

    def test_page_has_footer_wired_to_collector_meta(self):
        page = (self.WEB / "index.html").read_text(encoding="utf-8")
        script = (self.WEB / "footer.js").read_text(encoding="utf-8")
        self.assertIn('id="collector-lag"', page)
        self.assertIn('id="collector-version"', page)
        self.assertIn('src="footer.js"', page)
        self.assertNotIn("<script>", page)
        # The footer is added to the real Control Room page, which keeps its own assets.
        for asset in ('href="style.css"', 'src="lib.js"', 'src="app.js"', 'id="kpis"'):
            self.assertIn(asset, page)
        lib = (self.WEB / "lib.js").read_text(encoding="utf-8")
        self.assertIn("STALE_AFTER_S = 180, OLD_AFTER_S = 900", lib)
        self.assertIn("STALE_SECONDS = 180", script)
        self.assertIn("OLD_SECONDS = 900", script)
        self.assertIn("meta.asOf", script)
        self.assertIn("collector.version", script)
        self.assertIn("/api/snapshot", script)


if __name__ == "__main__":
    unittest.main()
