import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError

from controlroom.http import get, get_json


class FakeHandler(BaseHTTPRequestHandler):
    request_methods = []
    request_paths = []
    retry_requests = 0

    def do_GET(self):
        type(self).request_methods.append(self.command)
        type(self).request_paths.append(self.path)
        if self.path == "/retry":
            type(self).retry_requests += 1
            if type(self).retry_requests == 1:
                self.send_response(503)
                self.end_headers()
                return
            body = b'{"ok": true}'
        elif self.path == "/ftp-redirect":
            self.send_response(302)
            self.send_header("Location", "ftp://127.0.0.1:1/forbidden")
            self.end_headers()
            return
        elif self.path == "/slow":
            time.sleep(0.2)
            body = b"slow"
        else:
            body = b"body"

        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, format, *args):
        pass


class HttpTest(unittest.TestCase):
    def setUp(self):
        FakeHandler.request_methods = []
        FakeHandler.request_paths = []
        FakeHandler.retry_requests = 0

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = "http://127.0.0.1:{}".format(cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_retries_transient_status_and_returns_json(self):
        response = get_json(self.base_url + "/retry", retries=1, backoff=0)

        self.assertEqual(response, {"ok": True})
        self.assertEqual(FakeHandler.retry_requests, 2)
        self.assertEqual(FakeHandler.request_methods, ["GET", "GET"])

    def test_timeout_is_enforced(self):
        with self.assertRaises((URLError, TimeoutError)):
            get(self.base_url + "/slow", timeout=0.05, retries=0, backoff=0)
        self.assertTrue(all(method == "GET" for method in FakeHandler.request_methods))

    def test_rejects_non_http_url_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.test/file"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "http or https"):
                    get(url, retries=0)

    def test_rejects_ftp_redirect_before_following_it(self):
        with self.assertRaises(HTTPError) as error:
            get(self.base_url + "/ftp-redirect", retries=0)

        self.assertEqual(
            error.exception.msg, "redirect target must use http or https"
        )
        self.assertEqual(FakeHandler.request_paths.count("/ftp-redirect"), 1)


if __name__ == "__main__":
    unittest.main()
