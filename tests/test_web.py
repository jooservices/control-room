"""Smoke tests of the static page. No JavaScript engine is available, so the page is served by the real
server, parsed with html.parser, and its sources are checked for the contract the ACs describe."""

import http.client
import re
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path

from controlroom.server import make_server

WEB = Path(__file__).resolve().parent.parent / "web"
PLACEHOLDER_IDS = {
    "live", "liveText", "srcwarn", "theme", "kpis", "tl", "tlnote", "legend", "fun", "funnote", "ptable",
    "aitable", "ainote", "machine", "gauges", "spark", "mkv", "chips", "event-project", "feed", "scrim", "drawer",
}


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.assets, self.inline_scripts, self.bad_attrs = set(), [], 0, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "script":
            if "src" in attrs:
                self.assets.append(attrs["src"])
            else:
                self.inline_scripts += 1
        if tag == "link":
            self.assets.append(attrs.get("href", ""))
        for name in attrs:
            if name == "style" or name.startswith("on"):
                self.bad_attrs.append((tag, name))


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        snapshot = Path(cls.tmp.name) / "snapshot.json"
        snapshot.write_text('{"version":1}', encoding="utf-8")
        cls.server = make_server("127.0.0.1", 0, web_dir=WEB, snapshot_path=snapshot, interval_seconds=10)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.tmp.cleanup()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8")

    def test_served_page_has_every_placeholder_and_local_assets_only(self):
        status, html = self.get("/")
        self.assertEqual(status, 200)
        parser = PageParser()
        parser.feed(html)
        self.assertEqual(PLACEHOLDER_IDS - parser.ids, set())
        self.assertEqual(sorted(parser.assets), ["app.js", "footer.css", "footer.js", "lib.js", "style.css"])
        self.assertEqual(parser.inline_scripts, 0, "the CSP only allows script files of the same origin")
        self.assertEqual(parser.bad_attrs, [], "the CSP forbids inline styles and handlers")
        for asset in parser.assets:
            status, body = self.get("/" + asset)
            self.assertEqual(status, 200, asset)
            self.assertTrue(body.strip(), asset)

    def test_no_external_request_and_no_demo_store(self):
        for name in ("index.html", "app.js", "lib.js", "style.css"):
            text = (WEB / name).read_text(encoding="utf-8")
            self.assertNotIn("fonts.googleapis", text, name)
            self.assertNotRegex(text, r"@import|url\(\s*['\"]?https?:", name)
            self.assertNotRegex(text, r"claude\.use|BOOT|/\*DOCS\*/", name)
        self.assertNotRegex((WEB / "index.html").read_text(encoding="utf-8"), r"(src|href)=\"(https?:)?//")
        # the only absolute URLs the scripts may hold are navigation links to Jira and GitHub
        for name in ("app.js", "lib.js"):
            urls = set(re.findall(r"https?://[^'\"/ ]+", (WEB / name).read_text(encoding="utf-8")))
            self.assertLessEqual(urls, {"https://jooservices.atlassian.net", "https://github.com"}, name)

    def test_data_flow_endpoints_and_polling_interval(self):
        lib = (WEB / "lib.js").read_text(encoding="utf-8")
        app = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn("'/api/snapshot'", lib)
        self.assertIn("'/api/stream'", lib)
        self.assertRegex(lib, r"POLL_MS = 30000\b")
        self.assertIn("new EventSource(", app)
        self.assertIn("CRLib.createFeed(", app)
        self.assertIn("onerror", lib)  # the stream dropping starts the polling and a later reconnect

    def test_badge_uses_meta_as_of_with_amber_and_red_thresholds(self):
        lib = (WEB / "lib.js").read_text(encoding="utf-8")
        app = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertRegex(lib, r"STALE_AFTER_S = 180\b")
        self.assertRegex(lib, r"OLD_AFTER_S = 900\b")
        self.assertIn("DOCS.meta.asOf", app)
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertIn(".live.stale i { background: var(--wait)", css)
        self.assertIn(".live.old i { background: var(--blocked)", css)

    def test_source_failures_and_eta_fallback_come_from_the_snapshot(self):
        app = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn("DOCS.meta.ok", app)
        self.assertIn("item.eta", app)
        self.assertIn("not enough data (", app)
        self.assertNotIn("/ 2'", app)
        self.assertNotIn("n / 2", app)

    def test_project_and_epic_eta_show_ranges_samples_and_exclusions(self):
        app = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn("item.eta.low", app)
        self.assertIn("item.eta.middle", app)
        self.assertIn("item.eta.high", app)
        self.assertIn("item.etaSamples", app)
        self.assertIn("not enough data (", app)
        self.assertIn("Excluded from ETA", app)

    def test_app_generates_no_inline_handlers_and_guards_unknown_totals(self):
        app = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertNotRegex(app, r"""onclick=["']""")
        self.assertIn("a.runsAll == null ? 'not recorded'", app)

    def test_events_have_project_and_kind_filters_and_open_entity_drawers(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        app = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn('for="event-project"', html)
        self.assertIn('aria-label="Filter events by kind"', html)
        self.assertIn('role="dialog" aria-modal="true"', html)
        self.assertIn("projectFilter === 'all' || e.project === projectFilter", app)
        self.assertIn("openTarget(b.dataset.targetType, b.dataset.targetKey)", app)
        self.assertIn("e.key !== 'Tab'", app)
        self.assertIn("drawerOpener", app)

    def test_event_renderer_escapes_content_and_exposes_keyboard_actions(self):
        lib = (WEB / "lib.js").read_text(encoding="utf-8")
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertIn("function renderEvent(event, timeLabel)", lib)
        self.assertIn('class="event-open" data-target-type=', lib)
        self.assertIn('aria-label="Open ', lib)
        self.assertIn("esc(text)", lib)
        self.assertIn("CRLib.renderEvent(e, clock(e.at))", (WEB / "app.js").read_text(encoding="utf-8"))
        self.assertIn("@media (max-width: 480px)", css)
        self.assertIn(".event-open:focus-visible", css)


if __name__ == "__main__":
    unittest.main()
