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
    "live", "liveText", "srcbanner", "theme", "kpis", "tl", "tlnote", "legend", "fun", "funnote", "ptable",
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
        self.assertIn("CRLib.downSources(DOCS && DOCS.meta", app)
        self.assertIn("meta.ok", (WEB / "lib.js").read_text(encoding="utf-8"))
        self.assertIn("CRLib.downSources(DOCS && DOCS.meta", app)
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

    def test_banner_lists_down_sources_with_since_and_marks_panels_stale(self):
        lib = (WEB / "lib.js").read_text(encoding="utf-8")
        app = (WEB / "app.js").read_text(encoding="utf-8")
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertIn("meta.downSince", lib)
        self.assertIn("down since", app)
        self.assertIn("<b>stale</b>", app)  # a KPI tile shows the word, not the last known number
        cards = re.findall(r'<section class="card [^"]*" data-src="([^"]+)"><h2>[^<]*<span class="stale-tag" hidden>stale</span>', html)
        self.assertEqual(len(cards), 6, "every card names its sources and has a text stale tag")
        self.assertEqual({s for c in cards for s in c.split()}, {"jira", "github", "machine"})
        by_title = {
            t: set(src.split())
            for src, t in re.findall(r'<section class="card [^"]*" data-src="([^"]+)"><h2>([^<]*?) <span class="stale-tag"', html)
        }
        self.assertEqual(by_title, {
            "AI runs": {"machine", "github"},
            "Tickets now": {"jira", "machine"},  # flow tickets depend on the machine-selected flowProjects
            "Projects": {"jira", "github", "machine"},
            "AI models": {"machine"},
            "Machine": {"machine"},
            "Events": {"jira", "github", "machine"},
        })
        kpis = {
            label: set(re.findall(r"'(jira|github|machine)'", srcs))
            for label, srcs in re.findall(r"\['([^']+)', .*?, \[((?:'[a-z]+',? ?)+)\]\]", app)
        }
        self.assertEqual(kpis, {
            "Working now": {"machine"},
            "In the flow": {"jira", "machine"},
            "Waiting for owner": {"jira", "machine"},
            "Done, 24 hours": {"jira", "machine"},
            "Merged PRs, 24 hours": {"github", "machine"},
            "Typical time": {"jira"},
            "Rework, 24 hours": {"jira"},
            "Machine load": {"machine"},
        })

    def test_keyboard_and_phone_hooks(self):
        app = (WEB / "app.js").read_text(encoding="utf-8")
        css = (WEB / "style.css").read_text(encoding="utf-8")
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertEqual(app.count('<tr class="row" tabindex="0"'), 2)
        self.assertIn('class="seg-bar"', app)
        self.assertIn('tabindex="0" role="button"', app)
        self.assertIn("e.key !== 'Enter'", app)
        self.assertIn(":focus-visible {", css)
        self.assertIn("visibility: hidden", css)  # a closed drawer is out of the tab order
        self.assertIn('role="dialog"', html)
        self.assertRegex(css, r"@media \(max-width: 480px\)")
        self.assertIn("overflow-x: hidden", css)
        self.assertIn("width=device-width, initial-scale=1", html)

    def test_stale_state_does_not_dim_text_below_contrast(self):
        css = (WEB / "style.css").read_text(encoding="utf-8")
        for rule in re.findall(r"[^{}]*is-stale[^{}]*\{([^}]*)\}", css):
            self.assertNotIn("opacity", rule)
        for theme, palette in themes().items():
            for fg in ("ink", "ink-2", "blocked"):
                ratio = contrast(palette[fg], palette["panel"])
                self.assertGreaterEqual(ratio, 4.5, "{} stale {} on panel: {:.2f}".format(theme, fg, ratio))

    def test_timeline_labels_are_not_dimmed_and_pass_on_every_role_bar(self):
        app = (WEB / "app.js").read_text(encoding="utf-8")
        for tag in re.findall(r"<text class=\"seg-t\"[^>]*>", app):
            self.assertNotIn("opacity", tag)
        css = (WEB / "style.css").read_text(encoding="utf-8")
        self.assertNotIn("opacity", re.search(r"\.seg-t \{([^}]*)\}", css).group(1))
        for theme, palette in themes().items():
            for bar in ("run", "todo", "wait"):
                ratio = contrast(palette["on-color"], palette[bar])
                self.assertGreaterEqual(ratio, 4.5, "{} timeline label on {}: {:.2f}".format(theme, bar, ratio))

    def test_text_contrast_is_at_least_4_5_to_1_in_both_themes(self):
        for theme, palette in themes().items():
            for fg, bgs in TEXT_ON.items():
                for bg in bgs:
                    ratio = contrast(palette[fg], palette[bg])
                    self.assertGreaterEqual(ratio, 4.5, "{} {} on {}: {:.2f}".format(theme, fg, bg, ratio))
            for status in ("todo", "run", "wait", "human", "blocked", "done", "backlog"):
                ratio = contrast(palette["on-color"], palette[status])
                self.assertGreaterEqual(ratio, 4.5, "{} pill text on {}: {:.2f}".format(theme, status, ratio))


def css_vars(block):
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


def themes():
    css = (WEB / "style.css").read_text(encoding="utf-8")
    dark = css_vars(re.search(r":root \{(.*?)\n\}", css, re.S).group(1))
    light = css_vars(re.search(r':root\[data-theme="light"\] \{(.*?)\}', css, re.S).group(1))
    media = css_vars(re.search(r"@media \(prefers-color-scheme: light\) \{ :root:not\(\[data-theme=\"dark\"\]\) \{(.*?)\} \}", css, re.S).group(1))
    assert light == media, "the system-light palette and the chosen-light palette must match"
    return {"dark": dark, "light": {**dark, **light}}


def luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# text colour -> the surfaces it is drawn on
TEXT_ON = {
    "ink": ("bg", "panel", "panel-2"),
    "ink-2": ("bg", "panel", "panel-2", "grid"),
    "accent": ("bg", "panel", "panel-2"),
    "blocked": ("bg", "panel"),
    "wait": ("bg",),
    "on-color": ("accent",),
}


if __name__ == "__main__":
    unittest.main()
