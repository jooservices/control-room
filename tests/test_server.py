import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

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
        self.server = make_server(
            "127.0.0.1", 0, web_dir=self.web, snapshot_path=self.snapshot,
            interval_seconds=10,
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

    def test_lan_address_is_not_wildcard(self):
        self.assertNotEqual(lan_address(), "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
