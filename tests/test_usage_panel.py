"""Aggregation of the stored run log (AI usage panel) over the usage-log fixture."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from controlroom import collector
from controlroom.config import Config
from controlroom.history import HistoryStore
from controlroom.sources.machine import parse_usage_log
from test_collector import NOW, all_results

FIXTURE = Path(__file__).parent / "fixtures" / "machine" / "usage-log.json"
FIXTURE_NOW = 2_000_000_000
DAY = 86400


def run(age, ai, role, minutes, why="ok", ticket="A-1", now=FIXTURE_NOW):
    end = now - age
    return {"name": "usage:%s:%s:%d" % (ticket, ai, end), "end": end,
            "ai": ai, "role": role, "minutes": minutes, "why": why}


class UsagePanelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = HistoryStore(Path(tmp.name) / "history.db")

    def fixture_usage(self):
        return parse_usage_log(
            json.loads(FIXTURE.read_text()), now=FIXTURE_NOW, window_seconds=40 * DAY)

    def test_aggregates_per_ai_and_role_over_three_windows(self):
        self.store.record_runs([
            run(60, "Codex", "developer", 3.5, ticket="A-1"),
            run(2 * DAY, "Codex", "developer", 10, why="timeout", ticket="A-2"),
            run(20 * DAY, "Codex", "developer", 5, ticket="A-3"),
            run(3600, "Claude", "reviewer", 2, ticket="A-4"),
            run(40 * DAY, "Claude", "reviewer", 9, ticket="A-5"),
        ])
        rows = {(r["ai"], r["role"]): r for r in self.store.usage_panel(FIXTURE_NOW)}
        self.assertEqual(set(rows), {("Codex", "developer"), ("Claude", "reviewer")})
        codex = rows[("Codex", "developer")]
        for window, expected in (("24h", (1, 3.5, 0)), ("7d", (2, 13.5, 1)), ("30d", (3, 18.5, 1))):
            self.assertEqual(
                (codex["runs" + window], codex["minutes" + window], codex["bad" + window]), expected)
        claude = rows[("Claude", "reviewer")]
        self.assertEqual((claude["runs30d"], claude["minutes30d"]), (1, 2.0))

    def test_fixture_log_counts_valid_runs_only(self):
        self.assertEqual(self.store.record_runs(self.fixture_usage()), 3)
        rows = {(r["ai"], r["role"]): r for r in self.store.usage_panel(FIXTURE_NOW)}
        codex = rows[("Codex", "developer")]
        self.assertEqual((codex["runs24h"], codex["runs7d"]), (1, 2))
        claude = rows[("Claude", "reviewer")]
        self.assertEqual((claude["runs24h"], claude["runs7d"]), (0, 1))

    def test_recording_the_same_run_twice_stores_it_once(self):
        self.assertEqual(self.store.record_runs(self.fixture_usage()), 3)
        self.assertEqual(self.store.record_runs(self.fixture_usage()), 0)
        self.assertEqual(sum(r["runs30d"] for r in self.store.usage_panel(FIXTURE_NOW)), 3)

    def test_prune_drops_runs_older_than_retention(self):
        self.store.record_runs([run(100 * DAY, "Codex", "developer", 1)])
        self.store.prune(dt.datetime.fromtimestamp(FIXTURE_NOW, dt.timezone.utc))
        self.assertEqual(self.store.usage_panel(FIXTURE_NOW - 100 * DAY + 60), [])

    def test_run_once_adds_usage_rows_to_the_snapshot_without_double_counting(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                jira_site="https://x.example", jira_user="u", jira_token="t",
                github_repos=("a/b",), data_dir=Path(tmp), interval_seconds=45,
            )
            collector.run_once(config, now=NOW, collect=lambda *_: all_results())
            second = collector.run_once(
                config, now=NOW + dt.timedelta(seconds=45), collect=lambda *_: all_results())
        rows = second["ai"]["usage"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["ai"], rows[0]["role"]), ("Claude", "developer"))
        self.assertEqual((rows[0]["runs24h"], rows[0]["minutes30d"]), (1, 4.0))


if __name__ == "__main__":
    unittest.main()
