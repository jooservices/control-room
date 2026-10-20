import base64
import datetime as dt
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from controlroom.config import Config
from controlroom.sources import events


NOW = dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures" / "events" / "search_30d.json"


def search_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def configured():
    return Config(
        jira_site="https://jira.example.test",
        jira_user="jira-user",
        jira_token="jira-token",
        github_repos=("owner/repo",),
        data_dir=Path("data"),
        interval_seconds=45,
        openobserve_url="https://observe.example.test",
        openobserve_org="flow",
        openobserve_user="openobserve-user",
        openobserve_password="openobserve-password",
    )


class ParseEventsTest(unittest.TestCase):
    def test_parses_status_medians_and_ai_role_usage_for_all_windows(self):
        result = events.parse_query_result(search_fixture(), now=NOW)

        self.assertEqual(
            result["statusMedianMinutes"],
            {
                "24h": {"In Progress": 120.0, "Review": 210.0},
                "7d": {
                    "In Progress": 120.0,
                    "Ready": 1440.0,
                    "Review": 210.0,
                },
                "30d": {
                    "Build": 1440.0,
                    "In Progress": 120.0,
                    "Ready": 1440.0,
                    "Review": 210.0,
                },
            },
        )
        self.assertEqual(
            result["usageByRole"],
            {
                "24h": [
                    {
                        "ai": "Codex",
                        "role": "developer",
                        "runs": 1,
                        "minutes": 2.0,
                        "tokens": 150,
                    }
                ],
                "7d": [
                    {
                        "ai": "Codex",
                        "role": "developer",
                        "runs": 2,
                        "minutes": 7.0,
                        "tokens": 400,
                    }
                ],
                "30d": [
                    {
                        "ai": "Claude",
                        "role": "reviewer",
                        "runs": 1,
                        "minutes": 10.0,
                        "tokens": 400,
                    },
                    {
                        "ai": "Codex",
                        "role": "developer",
                        "runs": 2,
                        "minutes": 7.0,
                        "tokens": 400,
                    },
                ],
            },
        )

    def test_rejects_malformed_recognized_event_instead_of_returning_partial_data(self):
        with self.assertRaisesRegex(ValueError, "token count"):
            events.parse_query_result(
                {
                    "hits": [
                        {
                            "event": "ai.usage",
                            "at": "2026-10-20T11:00:00Z",
                            "ai": "Codex",
                            "role": "developer",
                        }
                    ]
                },
                now=NOW,
            )

    def test_rejects_invalid_search_response(self):
        with self.assertRaisesRegex(ValueError, "invalid hits"):
            events.parse_query_result({"hits": {}}, now=NOW)


class CollectEventsTest(unittest.TestCase):
    def test_collect_uses_basic_auth_and_a_30_day_search_window(self):
        fixture = search_fixture()
        with patch(
            "controlroom.sources.events.http.search_json",
            return_value=fixture,
        ) as search:
            result = events.collect(configured(), now=NOW)

        endpoint, payload = search.call_args.args
        request_query = payload["query"]
        self.assertEqual(
            endpoint, "https://observe.example.test/api/flow/_search"
        )
        self.assertEqual(request_query["from"], 0)
        self.assertEqual(request_query["size"], events.PAGE_SIZE)
        self.assertEqual(
            request_query["start_time"],
            int((NOW - dt.timedelta(days=30)).timestamp() * 1_000_000),
        )
        self.assertEqual(request_query["end_time"], int(NOW.timestamp() * 1_000_000))
        self.assertIn('"aiflow_events"', request_query["sql"])
        auth = search.call_args.kwargs["headers"]["Authorization"]
        expected = base64.b64encode(b"openobserve-user:openobserve-password").decode(
            "ascii"
        )
        self.assertEqual(auth, "Basic " + expected)
        self.assertEqual(result["usageByRole"]["24h"][0]["tokens"], 150)

    def test_search_paginates_until_the_reported_total_is_fetched(self):
        first_page = {"hits": [{"id": 1}, {"id": 2}], "total": 3}
        second_page = {"hits": [{"id": 3}], "total": 3}
        with patch.object(events, "PAGE_SIZE", 2), patch(
            "controlroom.sources.events.http.search_json",
            side_effect=[first_page, second_page],
        ) as search:
            rows = events._search_rows(
                "https://observe.example.test/api/flow/_search",
                {},
                start_us=1,
                end_us=2,
            )

        self.assertEqual(rows, [{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertEqual(
            [call.args[1]["query"]["from"] for call in search.call_args_list], [0, 2]
        )

    def test_incomplete_search_page_is_a_source_failure(self):
        with patch(
            "controlroom.sources.events.http.search_json",
            return_value={"hits": [], "total": 1},
        ):
            with self.assertRaisesRegex(ValueError, "incomplete page"):
                events._search_rows(
                    "https://observe.example.test/api/flow/_search",
                    {},
                    start_us=1,
                    end_us=2,
                )

    def test_missing_credentials_raise_without_a_network_request(self):
        config = configured()
        config = Config(
            jira_site=config.jira_site,
            jira_user=config.jira_user,
            jira_token=config.jira_token,
            github_repos=config.github_repos,
            data_dir=config.data_dir,
            interval_seconds=config.interval_seconds,
        )
        with patch("controlroom.sources.events.http.search_json") as search:
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                events.collect(config, now=NOW)
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
