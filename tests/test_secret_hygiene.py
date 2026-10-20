import datetime as dt
import re
import unittest

from controlroom.assembler import assemble


NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.timezone.utc)
EPOCH = int(NOW.timestamp())

_SECRET_PATTERNS = (
    (
        "Jira API token",
        re.compile(r"\bATATT[A-Za-z0-9_-]{20,}\b"),
        "ATATT3xFfGF0jiraFixtureToken0123456789",
    ),
    (
        "GitHub token",
        re.compile(
            r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b"
        ),
        "ghp_0123456789abcdefghijklmnopqrstuv",
    ),
    (
        "Redis password",
        re.compile(
            r"(?i)(?:\bredis[_ -]?(?:password|passwd)\s*[:=]\s*[^\s,;]+|"
            r"redis://[^/:@\s]*:[^/@\s]+@)"
        ),
        "redis_password=fixture-only-password",
    ),
    (
        "private key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    ),
)


def _snapshot_strings(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield from _snapshot_strings(key)
            yield from _snapshot_strings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _snapshot_strings(nested)
    elif isinstance(value, str):
        yield value


def _assert_snapshot_has_no_credentials(snapshot):
    for value in _snapshot_strings(snapshot):
        for _name, pattern, _example in _SECRET_PATTERNS:
            if pattern.search(value):
                raise AssertionError("snapshot contains a credential-like value")


def _safe_source_results():
    project = {
        "key": "JOOCR",
        "name": "Control Room",
        "repo": "control-room",
        "board": None,
        "total": 0,
        "counts": {},
        "epics": [],
        "active": [],
        "recent": [],
        "doneLast24h": 0,
        "mergedPRs": [],
        "openPRs": [],
        "lastUpdate": "",
        "onFlow": True,
    }
    return {
        "jira": ({"list": [project]}, []),
        "github": {
            "projects": {
                "JOOCR": {
                    "repo": "jooservices/control-room",
                    "openPRs": [],
                    "mergedPRs": [],
                    "ciRuns": [],
                }
            },
            "error": None,
        },
        "machine": {
            "now": EPOCH,
            "host": "worker.internal.example",
            "cpus": 4,
            "load": [0.1, 0.2, 0.3],
            "mem": {"total": 8.0, "avail": 4.0},
            "disk": {"total": 100, "used": 50},
            "uptime_h": 24.0,
            "containers": 2,
            "services": {},
            "hb": {},
            "leases": {},
            "probe": {
                "wsl:Codex": "down: {}".format(_SECRET_PATTERNS[1][2])
            },
            "cooldown": {},
            "usage": [
                {
                    "name": "usage:JOOCR-24:Codex:1999999999",
                    "end": EPOCH - 1,
                    "ai": "Codex",
                    "role": "developer",
                    "minutes": 1,
                    "why": "redis diagnostic {}".format(_SECRET_PATTERNS[2][2]),
                }
            ],
            "flowProjects": ["JOOCR"],
            "code": "abc123",
            "redis": None,
        },
    }


class SecretHygieneTest(unittest.TestCase):
    def test_full_snapshot_contains_no_credential_like_values(self):
        snapshot = assemble(_safe_source_results(), None, now=NOW)

        _assert_snapshot_has_no_credentials(snapshot)
        self.assertEqual(snapshot["machine"]["host"], "Workstation")
        self.assertEqual(snapshot["ai"]["agents"][0]["probe"], "down")
        events = snapshot["events"]["list"]
        self.assertTrue(any(event["kind"] == "badrun" for event in events))
        self.assertTrue(
            all(
                _SECRET_PATTERNS[2][2] not in event["text"]
                for event in events
            )
        )

    def test_scanner_recognizes_jira_github_redis_and_private_key_shapes(self):
        for name, pattern, example in _SECRET_PATTERNS:
            with self.subTest(kind=name):
                self.assertIsNotNone(pattern.search(example))

    def test_scanner_walks_nested_snapshot_values(self):
        snapshot = {"machine": {"history": [{"details": ""}]}}
        snapshot["machine"]["history"][0]["details"] = _SECRET_PATTERNS[1][2]

        with self.assertRaisesRegex(AssertionError, "credential-like"):
            _assert_snapshot_has_no_credentials(snapshot)


if __name__ == "__main__":
    unittest.main()
