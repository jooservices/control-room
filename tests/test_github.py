from datetime import datetime, timezone
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from controlroom.sources.github import collect, parse_gh_output


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "github"
NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
PROJECT_REPOS = {"JOOCR": "jooservices/control-room"}


def load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class GitHubParserTest(unittest.TestCase):
    def test_parses_open_prs_recent_merges_and_latest_ci_runs(self):
        result = parse_gh_output(
            load_fixture("open_prs.json"),
            load_fixture("merged_prs.json"),
            load_fixture("ci_runs.json"),
            now=NOW,
        )

        self.assertEqual(
            result["openPRs"],
            [
                {
                    "number": 42,
                    "title": "Add GitHub source",
                    "url": "https://github.com/jooservices/control-room/pull/42",
                    "draft": False,
                },
                {
                    "number": 41,
                    "title": "WIP: collector source",
                    "url": "https://github.com/jooservices/control-room/pull/41",
                    "draft": True,
                },
            ],
        )
        self.assertEqual(
            result["mergedPRs"],
            [
                {
                    "number": 40,
                    "title": "Add source foundation",
                    "mergedAt": "2026-10-20T11:30:00Z",
                    "url": "https://github.com/jooservices/control-room/pull/40",
                },
                {
                    "number": 38,
                    "title": "Earlier recent merge",
                    "mergedAt": "2026-10-20T10:30:00Z",
                    "url": "https://github.com/jooservices/control-room/pull/38",
                },
            ],
        )
        self.assertEqual(
            result["ciRuns"],
            [
                {
                    "name": "Build for pull request #1003",
                    "url": "https://github.com/jooservices/control-room/actions/runs/1003",
                    "status": "failure",
                    "mins": 4.0,
                },
                {
                    "name": "CI",
                    "url": "https://github.com/jooservices/control-room/actions/runs/1002",
                    "status": "success",
                    "mins": 6.5,
                },
                {
                    "name": "CI",
                    "url": "https://github.com/jooservices/control-room/actions/runs/1001",
                    "status": "in_progress",
                    "mins": 2.0,
                },
            ],
        )

    def test_rejects_invalid_json_shape(self):
        with self.assertRaisesRegex(ValueError, "JSON array"):
            parse_gh_output("{}", "[]", "[]", now=NOW)


class GitHubSourceTest(unittest.TestCase):
    def test_runs_read_only_gh_queries_and_returns_project_mapping(self):
        responses = [
            load_fixture("open_prs.json"),
            load_fixture("merged_prs.json"),
            load_fixture("ci_runs.json"),
        ]
        completed = [
            subprocess.CompletedProcess([], 0, output, "") for output in responses
        ]

        with patch(
            "controlroom.sources.github.subprocess.run", side_effect=completed
        ) as run:
            result = collect(PROJECT_REPOS, now=NOW)

        self.assertIsNone(result["error"])
        self.assertEqual(
            result["projects"]["JOOCR"]["repo"], PROJECT_REPOS["JOOCR"]
        )
        self.assertEqual(len(result["projects"]["JOOCR"]["mergedPRs"]), 2)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertEqual(
            [command[1:3] for command in commands],
            [["pr", "list"], ["pr", "list"], ["run", "list"]],
        )
        self.assertTrue(all("--json" in command for command in commands))
        self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list))
        self.assertIn("merged:>=2026-10-19", commands[1])

    def test_missing_gh_returns_empty_data_and_an_error_note(self):
        with patch(
            "controlroom.sources.github.subprocess.run", side_effect=FileNotFoundError
        ):
            result = collect(PROJECT_REPOS)

        self.assertEqual(result["error"], "GitHub CLI 'gh' is not available.")
        self.assertEqual(
            result["projects"]["JOOCR"],
            {
                "repo": "jooservices/control-room",
                "openPRs": [],
                "mergedPRs": [],
                "ciRuns": [],
            },
        )

    def test_failing_command_returns_empty_data_and_a_source_error(self):
        failure = subprocess.CalledProcessError(1, ["gh", "pr", "list"])
        with patch(
            "controlroom.sources.github.subprocess.run", side_effect=failure
        ):
            result = collect(PROJECT_REPOS)

        self.assertEqual(
            result["error"],
            "GitHub command failed for jooservices/control-room (exit code 1).",
        )
        self.assertEqual(
            result["projects"]["JOOCR"],
            {
                "repo": "jooservices/control-room",
                "openPRs": [],
                "mergedPRs": [],
                "ciRuns": [],
            },
        )

    def test_invalid_api_json_returns_empty_data_and_an_error_note(self):
        with patch(
            "controlroom.sources.github.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "not-json", ""),
        ):
            result = collect(PROJECT_REPOS)

        self.assertIn("invalid JSON data", result["error"])
        self.assertEqual(result["projects"]["JOOCR"]["ciRuns"], [])


if __name__ == "__main__":
    unittest.main()
