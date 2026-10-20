import copy
import datetime as dt
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from controlroom.config import Config
from controlroom.sources.jira import (
    PROJECTS,
    collect_jira,
    parse_projects_document,
    parse_status_events,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"
NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.timezone.utc)
PROJECT_DEFINITIONS = {"DEMO": ("Sample project", "sample-repo", None)}


def read_fixture(name):
    with (FIXTURE_DIR / name).open(encoding="utf-8") as fixture:
        return json.load(fixture)


class JiraFixtureHandler(BaseHTTPRequestHandler):
    request_paths = []
    search_page_1 = read_fixture("jira_search_page_1.json")
    search_page_2 = read_fixture("jira_search_page_2.json")
    changelog_pages = [
        read_fixture("jira_changelog_page_1.json"),
        read_fixture("jira_changelog_page_2.json"),
        read_fixture("jira_changelog_page_3.json"),
    ]

    def do_GET(self):
        type(self).request_paths.append(self.path)
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/rest/api/3/search/jql":
            token = query.get("nextPageToken", [None])[0]
            if token is None:
                self._send_json(type(self).search_page_1)
            elif token == "next page/+2":
                self._send_json(type(self).search_page_2)
            else:
                self._send_json({"error": "unexpected page token"}, status=400)
            return

        if parsed.path.startswith("/rest/api/3/issue/") and parsed.path.endswith(
            "/changelog"
        ):
            issue_key = parsed.path.split("/")[-2]
            if issue_key != "DEMO-2":
                self._send_json(
                    {"startAt": 0, "total": 0, "isLast": True, "values": []}
                )
                return
            start_at = int(query.get("startAt", ["0"])[0])
            if start_at < len(type(self).changelog_pages):
                self._send_json(type(self).changelog_pages[start_at])
            else:
                self._send_json({"error": "unexpected changelog page"}, status=400)
            return

        self._send_json({"error": "unexpected request"}, status=404)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class JiraSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), JiraFixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.site = "http://127.0.0.1:{}".format(cls.server.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        JiraFixtureHandler.request_paths = []

    def make_config(self):
        return Config(
            jira_site=self.site,
            jira_user="fixture-user",
            jira_token="fixture-token",
            github_repos=(),
            data_dir=Path("/tmp/control-room-test"),
            interval_seconds=30,
        )

    def test_project_metadata_includes_control_room(self):
        self.assertEqual(
            PROJECTS["JOOCR"], ("Control Room", "control-room", None)
        )

    def test_projects_document_counts_epic_children_and_recent_open_tickets(self):
        issues = (
            JiraFixtureHandler.search_page_1["issues"]
            + JiraFixtureHandler.search_page_2["issues"]
        )

        document = parse_projects_document(
            {"DEMO": issues}, ["DEMO"], now=NOW,
            project_definitions=PROJECT_DEFINITIONS,
        )

        project = document["list"][0]
        self.assertEqual(project["counts"], {
            "Declined": 1, "Done": 2, "In Progress": 2, "Open": 1
        })
        self.assertEqual(project["total"], 6)
        self.assertEqual(project["epics"], [{
            "key": "DEMO-1",
            "summary": "Anonymized release epic",
            "status": "In Progress",
            "children": 3,
            "done": 2,
            "_etaSamples": [],
            "_etaRemaining": 1,
            "_etaExcluded": [],
        }])
        self.assertEqual([ticket["key"] for ticket in project["active"]], [
            "DEMO-6", "DEMO-2"
        ])
        self.assertTrue(all(isinstance(t["labels"], list) for t in project["active"]))
        self.assertEqual(project["doneLast24h"], 1)
        self.assertTrue(project["onFlow"])
        self.assertEqual(project["_etaCycleCache"], {"DEMO-3": None, "DEMO-4": None})

    def test_eta_source_data_uses_finished_cycles_and_lists_excluded_work(self):
        epic = copy.deepcopy(JiraFixtureHandler.search_page_1["issues"][0])
        completed = copy.deepcopy(JiraFixtureHandler.search_page_1["issues"][2])
        active = copy.deepcopy(JiraFixtureHandler.search_page_1["issues"][1])
        epic_key = epic["key"]
        issues = [epic]
        histories = {}

        for index, minutes in enumerate((60, 120, 180, 240, 300), start=10):
            issue = copy.deepcopy(completed)
            issue["key"] = "DEMO-{}".format(index)
            issue["fields"]["parent"] = {"key": epic_key}
            issues.append(issue)
            start = NOW - dt.timedelta(minutes=minutes + 5)
            finish = NOW - dt.timedelta(minutes=5)
            histories[issue["key"]] = [
                {"created": start.isoformat(), "items": [{"field": "status", "toString": "In Progress"}]},
                {"created": finish.isoformat(), "items": [{"field": "status", "toString": "Done"}]},
            ]

        excluded = []
        for index, status in enumerate(("Blocked", "Human review", "Ready to merge"), start=30):
            issue = copy.deepcopy(active)
            issue["key"] = "DEMO-{}".format(index)
            issue["fields"]["parent"] = {"key": epic_key}
            issue["fields"]["status"] = {"name": status}
            issues.append(issue)
            excluded.append((issue["key"], status))

        remaining = copy.deepcopy(active)
        remaining["key"] = "DEMO-40"
        remaining["fields"]["parent"] = {"key": epic_key}
        remaining["fields"]["status"] = {"name": "In Progress"}
        issues.append(remaining)

        held = copy.deepcopy(active)
        held["key"] = "DEMO-41"
        held["fields"]["parent"] = {"key": epic_key}
        held["fields"]["labels"] = ["aiflow-hold"]
        issues.append(held)

        document = parse_projects_document(
            {"DEMO": issues}, ["DEMO"], histories_by_issue=histories,
            now=NOW, project_definitions=PROJECT_DEFINITIONS,
        )
        project = document["list"][0]

        self.assertEqual(project["_etaSamples"], [60, 120, 180, 240, 300])
        self.assertEqual(project["_etaRemaining"], 1)
        self.assertEqual(
            project["_etaCycleCache"],
            {"DEMO-10": 60, "DEMO-11": 120, "DEMO-12": 180, "DEMO-13": 240, "DEMO-14": 300},
        )
        self.assertEqual(
            [(item["key"], item["status"]) for item in project["_etaExcluded"]],
            excluded,
        )
        self.assertEqual(project["epics"][0]["_etaSamples"], [60, 120, 180, 240, 300])
        self.assertEqual(project["epics"][0]["_etaRemaining"], 1)
        self.assertEqual(len(project["epics"][0]["_etaExcluded"]), 3)

        non_flow = parse_projects_document(
            {"DEMO": issues}, [], histories_by_issue=histories,
            now=NOW, project_definitions=PROJECT_DEFINITIONS,
        )["list"][0]
        self.assertEqual(non_flow["_etaSamples"], [])
        self.assertEqual(non_flow["_etaRemaining"], 0)

    def test_projects_document_limits_open_tickets_to_thirty_most_recent(self):
        base_issue = JiraFixtureHandler.search_page_2["issues"][2]
        generated = []
        for index in range(35):
            issue = copy.deepcopy(base_issue)
            issue["key"] = "DEMO-{}".format(100 + index)
            issue["fields"]["updated"] = (
                NOW - dt.timedelta(minutes=index)
            ).isoformat()
            generated.append(issue)

        document = parse_projects_document(
            {"DEMO": generated}, [], now=NOW,
            project_definitions=PROJECT_DEFINITIONS,
        )

        active = document["list"][0]["active"]
        self.assertEqual(len(active), 30)
        self.assertEqual(active[0]["key"], "DEMO-100")
        self.assertEqual(active[-1]["key"], "DEMO-129")

    def test_status_events_include_only_status_changes_in_last_24_hours(self):
        issues = (
            JiraFixtureHandler.search_page_1["issues"]
            + JiraFixtureHandler.search_page_2["issues"]
        )
        histories = [page["values"][0] for page in JiraFixtureHandler.changelog_pages]

        events = parse_status_events(
            {"DEMO": issues}, {"DEMO-2": histories}, ["DEMO"], now=NOW
        )

        self.assertEqual(events, [
            {
                "at": "2026-10-20T11:30:00Z",
                "kind": "status",
                "key": "DEMO-2",
                "text": "In Progress → Done",
            },
            {
                "at": "2026-10-19T12:00:00Z",
                "kind": "status",
                "key": "DEMO-2",
                "text": "Open → Review",
            },
        ])

    def test_collection_pages_search_and_only_reads_flow_changelogs(self):
        projects, events = collect_jira(
            self.make_config(), ["DEMO"], now=NOW,
            project_definitions=PROJECT_DEFINITIONS,
        )

        paths = [urlsplit(path) for path in JiraFixtureHandler.request_paths]
        search_requests = [path for path in paths if path.path.endswith("/search/jql")]
        changelog_requests = [
            path for path in paths if path.path.endswith("/changelog")
        ]
        self.assertEqual(len(search_requests), 2)
        self.assertEqual(
            parse_qs(search_requests[1].query)["nextPageToken"], ["next page/+2"]
        )
        self.assertEqual(len(changelog_requests), 7)
        self.assertEqual(
            {path.path.split("/")[-2] for path in changelog_requests},
            {"DEMO-2", "DEMO-3", "DEMO-4", "DEMO-5", "DEMO-6"},
        )
        self.assertEqual(projects["list"][0]["key"], "DEMO")
        self.assertEqual([event["key"] for event in events], ["DEMO-2", "DEMO-2"])

    def test_collection_does_not_read_changelogs_without_flow_projects(self):
        projects, events = collect_jira(
            self.make_config(), [], now=NOW,
            project_definitions=PROJECT_DEFINITIONS,
        )

        self.assertEqual(projects["list"][0]["onFlow"], False)
        self.assertEqual(events, [])
        self.assertFalse(
            any("/changelog" in path for path in JiraFixtureHandler.request_paths)
        )

    def test_collection_reuses_cached_cycle_data_for_older_completed_tickets(self):
        projects, _events = collect_jira(
            self.make_config(), ["DEMO"], now=NOW,
            cycle_cache_by_project={"DEMO": {"DEMO-3": 120}},
            project_definitions=PROJECT_DEFINITIONS,
        )

        self.assertFalse(any("/DEMO-3/changelog" in path for path in JiraFixtureHandler.request_paths))
        self.assertIn(120, projects["list"][0]["_etaSamples"])
        self.assertEqual(projects["list"][0]["_etaCycleCache"]["DEMO-3"], 120)


if __name__ == "__main__":
    unittest.main()
