import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from controlroom import collector
from controlroom.assembler import assemble
from controlroom.config import Config, load_config
from controlroom.history import HistoryStore, snapshot_samples

NOW = dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
EPOCH = int(NOW.timestamp())


def jira_result():
    row = {
        "key": "JOOCR", "name": "Control Room", "repo": "control-room", "board": None,
        "total": 4, "counts": {"Done": 1, "To Do": 2, "Epic": 1},
        "epics": [{"key": "JOOCR-1", "summary": "e", "status": "Epic", "children": 3, "done": 1}],
        "active": [], "recent": [], "doneLast24h": 1, "mergedPRs": [], "openPRs": [],
        "lastUpdate": "2026-10-20T11:00", "onFlow": True,
    }
    event = {"at": "2026-10-20T11:00:00Z", "kind": "status", "key": "JOOCR-5", "text": "A → B"}
    return {"list": [row]}, [event]


def github_result():
    pr = {"number": 7, "title": "feat: x (JOOCR-5)", "mergedAt": "2026-10-20T10:00:00Z", "url": "u"}
    return {
        "projects": {
            "JOOCR": {
                "repo": "jooservices/control-room",
                "openPRs": [{"number": 8, "title": "t", "url": "u2", "draft": False}],
                "mergedPRs": [pr],
                "ciRuns": [{"name": "ci", "url": "u3", "status": "success", "mins": 1.5}],
            }
        },
        "error": None,
    }


def machine_result():
    return {
        "now": EPOCH, "hb": {"hb:w": 3}, "leases": {"JOOCR-5": "x"}, "probe": {"wsl:Claude": "ok"},
        "cooldown": {}, "load": [0.5, 0.4, 0.3], "cpus": 8, "mem": {"total": 16.0, "avail": 6.0},
        "disk": {"total": 10}, "uptime_h": 5.0, "containers": 2, "services": {"s": "active"},
        "code": "abc", "flowProjects": ["JOOCR"], "host": "wsl", "redis": None,
        "usage": [{"name": "usage:1", "end": EPOCH - 600, "key": "JOOCR-5", "role": "developer",
                   "ai": "Claude", "minutes": 4.0, "why": "ok"}],
    }


def all_results():
    return {"jira": jira_result(), "github": github_result(), "machine": machine_result()}


class AssembleTest(unittest.TestCase):
    def test_builds_every_document_from_fake_sources(self):
        snapshot = assemble(all_results(), None, now=NOW)

        self.assertEqual(snapshot["meta"]["asOf"], "2026-10-20T12:00:00Z")
        self.assertEqual(snapshot["meta"]["ok"], {"jira": True, "github": True, "machine": True})
        self.assertEqual(snapshot["meta"]["flowProjects"], ["JOOCR"])
        row = snapshot["projects"]["list"][0]
        self.assertEqual(row["mergedPRs"][0]["number"], 7)
        self.assertEqual(row["openPRs"][0]["number"], 8)
        self.assertEqual(snapshot["flow"]["watched"], ["JOOCR"])
        self.assertEqual(snapshot["flow"]["statusCounts"], {"Done": 1, "To Do": 2})
        self.assertEqual(snapshot["flow"]["mergedPRs24h"], 1)
        claude = next(a for a in snapshot["ai"]["agents"] if a["id"] == "claude")
        self.assertEqual((claude["runs24h"], claude["probe"]), (1, "ok"))
        self.assertEqual(snapshot["timeline"]["runs"][0]["start"], EPOCH - 600 - 240)
        self.assertEqual(snapshot["timeline"]["merges"][0]["key"], "JOOCR-5")
        self.assertEqual(snapshot["machine"]["workersBusy"], 1)
        self.assertEqual(snapshot["machine"]["runs"][0]["name"], "ci")
        self.assertEqual(
            sorted(e["kind"] for e in snapshot["events"]["list"]), ["merge", "run", "status"]
        )

    def test_failed_source_keeps_previous_document_and_others_update(self):
        first = assemble(all_results(), None, now=NOW)
        later = NOW + dt.timedelta(seconds=45)
        results = all_results()
        results["jira"] = RuntimeError("down")
        results["machine"]["load"] = [9.0, 0, 0]

        second = assemble(results, first, now=later)

        self.assertEqual(second["projects"]["list"], first["projects"]["list"])
        self.assertEqual(second["flow"]["statusCounts"], first["flow"]["statusCounts"])
        self.assertIn("status", [e["kind"] for e in second["events"]["list"]])
        self.assertEqual(second["machine"]["load"], [9.0, 0, 0])
        self.assertEqual(
            second["meta"]["ok"], {"jira": False, "github": True, "machine": True}
        )
        self.assertEqual(second["meta"]["errorAt"], {"jira": "2026-10-20T12:00:45Z"})
        self.assertEqual(second["meta"]["asOf"], "2026-10-20T12:00:45Z")

    def test_github_error_note_counts_as_failure_and_keeps_pull_requests(self):
        first = assemble(all_results(), None, now=NOW)
        results = all_results()
        results["github"] = {"projects": {}, "error": "boom"}

        second = assemble(results, first, now=NOW + dt.timedelta(seconds=45))

        self.assertFalse(second["meta"]["ok"]["github"])
        self.assertEqual(second["projects"]["list"][0]["mergedPRs"][0]["number"], 7)
        self.assertEqual(second["machine"]["runs"], first["machine"]["runs"])

    def test_total_failure_keeps_as_of_and_documents(self):
        first = assemble(all_results(), None, now=NOW)
        failed = {name: OSError("x") for name in ("jira", "github", "machine")}

        second = assemble(failed, first, now=NOW + dt.timedelta(minutes=5))

        self.assertEqual(second["meta"]["asOf"], first["meta"]["asOf"])
        self.assertEqual(second["meta"]["ok"], {"jira": False, "github": False, "machine": False})
        for name in ("projects", "flow", "ai", "events", "machine"):
            self.assertEqual(second[name], first[name])

    def test_recovered_source_clears_error_time(self):
        first = assemble({**all_results(), "jira": OSError()}, None, now=NOW)
        self.assertIn("jira", first["meta"]["errorAt"])
        second = assemble(all_results(), first, now=NOW + dt.timedelta(seconds=45))
        self.assertEqual(second["meta"]["errorAt"], {})


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = HistoryStore(Path(self.tmp.name) / "sub" / "history.db")

    def test_insert_and_query(self):
        self.store.insert(100, {"machine.load": 1.0, "machine.mem_used": 4.0})
        self.store.insert(200, {"machine.load": 2.0, "machine.mem_used": 5.0})

        self.assertEqual(self.store.query("machine.load"), [(100, 1.0), (200, 2.0)])
        self.assertEqual(self.store.query("machine.load", since=150), [(200, 2.0)])
        self.assertEqual(self.store.query("machine.load", limit=1), [(200, 2.0)])
        history = self.store.machine_history()
        self.assertEqual([(h["load"], h["mem"]) for h in history], [(1.0, 4.0), (2.0, 5.0)])

    def test_prune_removes_only_samples_older_than_90_days(self):
        old = int((NOW - dt.timedelta(days=91)).timestamp())
        edge = int((NOW - dt.timedelta(days=89)).timestamp())
        self.store.insert(old, {"m": 1.0})
        self.store.insert(edge, {"m": 2.0})

        self.assertEqual(self.store.prune(NOW), 1)
        self.assertEqual(self.store.query("m"), [(edge, 2.0)])

    def test_samples_only_for_successful_sources(self):
        snapshot = assemble({**all_results(), "jira": OSError()}, None, now=NOW)
        samples = snapshot_samples(snapshot)

        self.assertEqual(samples["machine.load"], 0.5)
        self.assertEqual(samples["machine.mem_used"], 10.0)
        self.assertFalse([m for m in samples if m.startswith(("flow.", "project."))])
        full = snapshot_samples(assemble(all_results(), None, now=NOW))
        self.assertEqual(full["project.JOOCR.done"], 1.0)


class WriteSnapshotTest(unittest.TestCase):
    def test_atomic_write_replaces_file_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "data" / "snapshot.json"
            collector.write_snapshot(target, {"a": 1})
            collector.write_snapshot(target, {"a": 2})

            self.assertEqual(json.loads(target.read_text()), {"a": 2})
            self.assertEqual(os.listdir(target.parent), ["snapshot.json"])

    def test_failed_write_keeps_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "snapshot.json"
            collector.write_snapshot(target, {"a": 1})
            with patch("controlroom.collector.os.replace", side_effect=OSError("x")):
                with self.assertRaises(OSError):
                    collector.write_snapshot(target, {"a": 2})

            self.assertEqual(json.loads(target.read_text()), {"a": 1})
            self.assertEqual(os.listdir(tmp), ["snapshot.json"])


class RunOnceTest(unittest.TestCase):
    def test_run_once_writes_snapshot_and_history_and_survives_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                jira_site="https://x.example", jira_user="u", jira_token="t",
                github_repos=("a/b",), data_dir=Path(tmp), interval_seconds=45,
            )
            collector.run_once(config, now=NOW, collect=lambda *_: all_results())
            later = NOW + dt.timedelta(seconds=45)
            failing = {**all_results(), "machine": OSError("x")}
            snapshot = collector.run_once(config, now=later, collect=lambda *_: failing)

            written = json.loads((Path(tmp) / "snapshot.json").read_text())
            self.assertEqual(written, snapshot)
            self.assertFalse(written["meta"]["ok"]["machine"])
            self.assertEqual(written["machine"]["load"], [0.5, 0.4, 0.3])
            store = HistoryStore(Path(tmp) / "history.db")
            self.assertEqual(len(store.query("machine.load")), 1)
            self.assertEqual(len(store.query("flow.done24h")), 2)

    def test_default_interval_is_45_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text(
                "JIRA_SITE=https://x.example\nJIRA_READ_ONLY_USER=u\n"
                "JIRA_READ_ONLY_TOKEN=t\nGITHUB_REPOS=a/b\nDATA_DIR=d\n"
            )
            self.assertEqual(load_config(env).interval_seconds, 45)


if __name__ == "__main__":
    unittest.main()
