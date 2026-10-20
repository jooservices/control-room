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
from controlroom.sources.machine import parse_usage_log
from controlroom.history import HistoryStore, snapshot_samples

NOW = dt.datetime(2026, 10, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
EPOCH = int(NOW.timestamp())


def jira_result():
    row = {
        "key": "JOOCR", "name": "Control Room", "repo": "control-room", "board": None,
        "total": 6, "counts": {"Done": 1, "To Do": 2, "Epic": 1, "Blocked": 1, "Human review": 1},
        "epics": [{
            "key": "JOOCR-1", "summary": "e", "status": "Epic", "children": 5, "done": 1,
            "_etaSamples": [10, 20, 30, 40, 50], "_etaRemaining": 2,
            "_etaExcluded": [{"key": "JOOCR-8", "summary": "wait", "status": "Human review"}],
        }],
        "active": [], "recent": [], "doneLast24h": 1, "mergedPRs": [], "openPRs": [],
        "lastUpdate": "2026-10-20T11:00", "onFlow": True,
        "_etaSamples": [1, 2, 3, 4, 5], "_etaRemaining": 2,
        "_etaExcluded": [
            {"key": "JOOCR-6", "summary": "blocked", "status": "Blocked"},
            {"key": "JOOCR-8", "summary": "waiting", "status": "Human review"},
        ],
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
        "disk": {"total": 10}, "uptime_h": 5.0, "containers": 2,
        "code": "abc", "flowProjects": ["JOOCR"], "host": "wsl", "redis": None,
        "services": {"aiflow-work@1.service": "active", "aiflow-work@2.service": "active"},
        "usage": parse_usage_log(
            {
                f"usage:JOOCR-5:Claude:{EPOCH - 600}": json.dumps(
                    {"role": "developer", "ai": "Claude", "minutes": 4.0, "why": "ok"}
                )
            },
            now=EPOCH,
        ),
    }


def events_result():
    return {
        "statusMedianMinutes": {
            "24h": {"In Progress": 120.0},
            "7d": {"In Progress": 120.0, "Ready": 1440.0},
            "30d": {"Build": 1440.0, "In Progress": 120.0, "Ready": 1440.0},
        },
        "usageByRole": {
            "24h": [
                {
                    "ai": "Codex",
                    "role": "developer",
                    "runs": 1,
                    "minutes": 2.0,
                    "tokens": 150,
                }
            ],
            "7d": [],
            "30d": [],
        },
    }


def all_results():
    return {
        "jira": jira_result(),
        "github": github_result(),
        "machine": machine_result(),
        "events": events_result(),
    }


class CollectSourcesTest(unittest.TestCase):
    def test_collect_sources_runs_openobserve_as_an_independent_source(self):
        config = Config(
            jira_site="https://jira.example.test",
            jira_user="user",
            jira_token="token",
            github_repos=("owner/repo",),
            data_dir=Path("data"),
            interval_seconds=45,
        )
        with (
            patch(
                "controlroom.collector.machine.collect",
                return_value={"flowProjects": ["JAIF"]},
            ),
            patch(
                "controlroom.collector.jira.collect_jira",
                return_value=jira_result(),
            ),
            patch(
                "controlroom.collector.github.collect",
                return_value=github_result(),
            ),
            patch(
                "controlroom.collector.events.collect",
                return_value=events_result(),
            ) as event_source,
        ):
            results = collector.collect_sources(config, None, now=NOW)

        event_source.assert_called_once_with(config, now=NOW)
        self.assertEqual(results["events"], events_result())


class WorkerTotalTests(unittest.TestCase):
    def test_counts_worker_units_and_is_unknown_without_them(self):
        from controlroom.assembler import _worker_total

        self.assertEqual(
            _worker_total({"aiflow-work@1.service": "active", "aiflow-work@2.service": "active", "aiflow-web.service": "active"}),
            2,
        )
        self.assertIsNone(_worker_total({"aiflow-web.service": "active"}))
        self.assertIsNone(_worker_total(None))

    def test_counts_the_units_of_the_machine_fixture(self):
        from controlroom.assembler import _worker_total
        from controlroom.sources.machine import parse_systemd_output

        fixture = Path(__file__).parent / "fixtures" / "machine" / "systemd-units.txt"
        services = parse_systemd_output(fixture.read_text(encoding="utf-8"))
        self.assertEqual(_worker_total(services), 2)


class CollectSourcesTest(unittest.TestCase):
    def test_previous_cycle_cache_is_passed_to_jira_collection(self):
        config = Config(
            jira_site="https://jira.example",
            jira_user="fixture-user",
            jira_token="fixture-token",
            github_repos=(),
            data_dir=Path("/tmp/control-room-test"),
            interval_seconds=30,
        )
        previous = {
            "projects": {
                "list": [{"key": "DEMO", "etaCycleCache": {"DEMO-1": 42.0}}]
            }
        }
        with patch.object(
            collector.machine, "collect", return_value={"flowProjects": ["DEMO"]}
        ), patch.object(
            collector.jira, "collect_jira", return_value=({"list": []}, [])
        ) as collect_jira, patch.object(
            collector.github, "collect", return_value={"projects": {}, "error": None}
        ):
            collector.collect_sources(config, previous, NOW)

        collect_jira.assert_called_once_with(
            config,
            ["DEMO"],
            now=NOW,
            cycle_cache_by_project={"DEMO": {"DEMO-1": 42.0}},
        )


class AssembleTest(unittest.TestCase):
    def test_lifetime_usage_totals_are_unknown_not_truncated(self):
        results = all_results()
        old = {"end": EPOCH - 3 * 86400, "ai": "Claude", "minutes": 9.0, "why": "ok"}
        results["machine"]["usage"] = results["machine"]["usage"] + [old]

        snapshot = assemble(results, None, now=NOW)

        claude = next(a for a in snapshot["ai"]["agents"] if a["id"] == "claude")
        self.assertEqual(claude["runs24h"], 1)
        self.assertEqual(
            (claude["runsAll"], claude["minutesAll"], claude["badAll"]), (None, None, None)
        )

    def test_builds_every_document_from_fake_sources(self):
        snapshot = assemble(all_results(), None, now=NOW)

        self.assertEqual(snapshot["meta"]["asOf"], "2026-10-20T12:00:00Z")
        self.assertEqual(
            snapshot["meta"]["ok"],
            {"jira": True, "github": True, "machine": True, "events": True},
        )
        self.assertEqual(snapshot["meta"]["flowProjects"], ["JOOCR"])
        row = snapshot["projects"]["list"][0]
        self.assertEqual(row["mergedPRs"][0]["number"], 7)
        self.assertEqual(row["openPRs"][0]["number"], 8)
        self.assertEqual(row["eta"], {"low": 2.0, "middle": 3.0, "high": 4.0})
        self.assertEqual(row["etaSamples"], 5)
        self.assertEqual([item["status"] for item in row["etaExcluded"]], ["Blocked", "Human review"])
        epic = row["epics"][0]
        self.assertEqual(epic["eta"], {"low": 20.0, "middle": 30.0, "high": 40.0})
        self.assertEqual(epic["etaSamples"], 5)
        self.assertEqual(epic["etaExcluded"][0]["status"], "Human review")
        self.assertEqual(snapshot["flow"]["watched"], ["JOOCR"])
        self.assertEqual(
            snapshot["flow"]["statusCounts"],
            {"Blocked": 1, "Done": 1, "Human review": 1, "To Do": 2},
        )
        self.assertEqual(snapshot["flow"]["mergedPRs24h"], 1)
        claude = next(a for a in snapshot["ai"]["agents"] if a["id"] == "claude")
        self.assertEqual((claude["runs24h"], claude["probe"]), (1, "ok"))
        self.assertEqual(snapshot["timeline"]["runs"][0]["start"], EPOCH - 600 - 240)
        self.assertEqual(snapshot["timeline"]["merges"][0]["key"], "JOOCR-5")
        self.assertEqual(snapshot["machine"]["workersBusy"], 1)
        self.assertEqual(snapshot["machine"]["workersTotal"], 2)
        self.assertEqual(snapshot["machine"]["host"], "Workstation")
        self.assertEqual(
            snapshot["flow"]["statusMedianMinutes"],
            events_result()["statusMedianMinutes"],
        )
        self.assertEqual(snapshot["ai"]["usageByRole"], events_result()["usageByRole"])
        self.assertEqual(snapshot["machine"]["runs"][0]["name"], "ci")
        self.assertEqual(
            sorted(e["kind"] for e in snapshot["events"]["list"]), ["merge", "run", "status"]
        )
        events = snapshot["events"]["list"]
        self.assertTrue(all(event.get("link") for event in events))
        self.assertEqual([event["kind"] for event in events], ["run", "status", "merge"])
        status = events[1]
        self.assertEqual(status["project"], "JOOCR")
        self.assertEqual(status["link"], {"type": "ticket", "key": "JOOCR-5"})
        run = events[0]
        self.assertEqual(run["project"], "JOOCR")
        self.assertEqual(run["link"], {"type": "ai", "key": "claude"})
        merge = events[2]
        self.assertEqual(merge["link"], {"type": "ticket", "key": "JOOCR-5"})
        self.assertEqual(merge["url"], "u")

    def test_only_flow_projects_receive_estimates(self):
        results = all_results()
        results["jira"][0]["list"].append({
            "key": "OTHER", "onFlow": False, "epics": [],
            "_etaSamples": [1, 2, 3, 4, 5], "_etaRemaining": 2,
            "_etaExcluded": [],
        })

        snapshot = assemble(results, None, now=NOW)

        other = next(row for row in snapshot["projects"]["list"] if row["key"] == "OTHER")
        self.assertIsNone(other["eta"])
        self.assertEqual(other["etaSamples"], 0)

    def test_events_are_newest_first_and_capped_at_200(self):
        results = all_results()
        projects, _events = results["jira"]
        results["jira"] = (projects, [
            {
                "at": (NOW - dt.timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "kind": "status",
                "key": "JOOCR-{}".format(offset + 1),
                "text": "To Do → In Progress",
            }
            for offset in range(205)
        ])

        events = assemble(results, None, now=NOW)["events"]["list"]

        self.assertEqual(len(events), 200)
        self.assertEqual(events[0]["key"], "JOOCR-1")
        self.assertEqual(events[-1]["key"], "JOOCR-200")
        self.assertEqual(
            [event["at"] for event in events],
            sorted((event["at"] for event in events), reverse=True),
        )
        self.assertTrue(all(event["project"] == "JOOCR" for event in events))

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
        old_status = next(e for e in second["events"]["list"] if e["kind"] == "status")
        self.assertEqual(old_status["link"], {"type": "ticket", "key": "JOOCR-5"})
        self.assertEqual(second["machine"]["load"], [9.0, 0, 0])
        self.assertEqual(
            second["meta"]["ok"],
            {"jira": False, "github": True, "machine": True, "events": True},
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
        failed = {
            name: OSError("x")
            for name in ("jira", "github", "machine", "events")
        }

        second = assemble(failed, first, now=NOW + dt.timedelta(minutes=5))

        self.assertEqual(second["meta"]["asOf"], first["meta"]["asOf"])
        self.assertEqual(
            second["meta"]["ok"],
            {"jira": False, "github": False, "machine": False, "events": False},
        )
        for name in ("projects", "flow", "ai", "events", "machine"):
            self.assertEqual(second[name], first[name])

    def test_recovered_source_clears_error_time(self):
        first = assemble({**all_results(), "jira": OSError()}, None, now=NOW)
        self.assertIn("jira", first["meta"]["errorAt"])
        second = assemble(all_results(), first, now=NOW + dt.timedelta(seconds=45))
        self.assertEqual(second["meta"]["errorAt"], {})

    def test_failed_events_source_keeps_only_event_metrics_stale(self):
        first = assemble(all_results(), None, now=NOW)
        results = all_results()
        results["events"] = RuntimeError("OpenObserve down")
        results["jira"][0]["list"][0]["counts"] = {"In Progress": 3}

        second = assemble(
            results, first, now=NOW + dt.timedelta(seconds=45)
        )

        self.assertEqual(
            second["flow"]["statusMedianMinutes"],
            first["flow"]["statusMedianMinutes"],
        )
        self.assertEqual(second["ai"]["usageByRole"], first["ai"]["usageByRole"])
        self.assertEqual(second["flow"]["statusCounts"], {"In Progress": 3})
        self.assertEqual(
            second["meta"]["ok"],
            {"jira": True, "github": True, "machine": True, "events": False},
        )
        self.assertEqual(second["meta"]["errorAt"], {"events": "2026-10-20T12:00:45Z"})


class FlowStateTest(unittest.TestCase):
    def results_with_active(self):
        jira = jira_result()
        jira[0]["list"][0]["active"] = [
            {"key": "JOOCR-5", "type": "Task", "status": "In Progress", "priority": "High",
             "summary": "s", "parent": None, "updated": "2026-10-20T11:00"},
            {"key": "JOOCR-6", "type": "Task", "status": "Backlog", "priority": "Low",
             "summary": "b", "parent": None, "updated": "2026-10-20T10:00"},
        ]
        return {"jira": jira, "github": github_result(), "machine": machine_result()}

    def test_inflight_excludes_held_tickets(self):
        results = self.results_with_active()
        active = results["jira"][0]["list"][0]["active"]
        active[0]["labels"] = ["aiflow-hold"]
        active.append(dict(active[1], key="JOOCR-7", status="In Progress", labels=["x"]))
        flow = assemble(results, None, now=NOW)["flow"]
        self.assertEqual([row["key"] for row in flow["inflight"]], ["JOOCR-7"])
        self.assertEqual(flow["inflight"][0]["labels"], ["x"])

    def test_inflight_comes_from_jira_active_and_machine_leases(self):
        flow = assemble(self.results_with_active(), None, now=NOW)["flow"]
        self.assertEqual([row["key"] for row in flow["inflight"]], ["JOOCR-5"])
        self.assertEqual(flow["inflight"][0]["lease"], "x")
        self.assertEqual(flow["inflight"][0]["status"], "In Progress")
        self.assertIsNone(flow["inflight"][0]["pr"])

    def test_uncollected_flow_fields_are_unknown_not_zero(self):
        flow = assemble(self.results_with_active(), None, now=NOW)["flow"]
        for name in ("paused", "qaslot"):
            self.assertIsNone(flow[name])
        self.assertIsNone(flow["cycle"]["n"])

    def test_done_by_ai_counts_done_transitions_of_ai_worked_tickets(self):
        results = self.results_with_active()
        results["jira"][1].extend([
            {"at": "2026-10-20T11:00:00Z", "kind": "status", "key": "JOOCR-5",
             "text": "In Review → Done"},
            {"at": "2026-10-20T11:30:00Z", "kind": "status", "key": "JOOCR-9",
             "text": "In Review → Done"},
            {"at": "2026-10-20T11:40:00Z", "kind": "status", "key": "JOOCR-5",
             "text": "To Do → In Progress"},
        ])
        first = assemble(results, None, now=NOW)
        self.assertEqual(first["flow"]["doneByAiLast24h"], 1)
        results["jira"] = RuntimeError("down")
        second = assemble(results, first, now=NOW)
        self.assertEqual(second["flow"]["doneByAiLast24h"], 1)

    def test_machine_only_update_refreshes_leases_on_kept_rows(self):
        first = assemble(self.results_with_active(), None, now=NOW)
        machine = machine_result()
        machine["leases"] = {}
        second = assemble(
            {"jira": RuntimeError("down"), "github": github_result(), "machine": machine},
            first, now=NOW,
        )
        self.assertEqual([r["key"] for r in second["flow"]["inflight"]], ["JOOCR-5"])
        self.assertIsNone(second["flow"]["inflight"][0]["lease"])


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
        self.assertTrue([m for m in full if m.startswith("ai.") and m.endswith(".minutes24h")])
        self.assertEqual(full["flow.rework24h"], 0.0)

    def test_rework_counts_tickets_sent_back_to_todo(self):
        results = all_results()
        at = "2026-10-20T11:00:00Z"
        results["jira"][1].extend([
            {"at": at, "kind": "status", "key": "JOOCR-6", "text": "In Review → To Do"},
            {"at": at, "kind": "status", "key": "JOOCR-6", "text": "QA → To Do"},
            {"at": at, "kind": "status", "key": "JOOCR-7", "text": " → To Do"},
            {"at": "2026-10-10T11:00:00Z", "kind": "status", "key": "JOOCR-8", "text": "QA → To Do"},
        ])
        snapshot = assemble(results, None, now=NOW)
        self.assertEqual(snapshot["flow"]["reworkLast24h"], 1)
        self.assertEqual(snapshot_samples(snapshot)["flow.rework24h"], 1.0)

    def test_rework_is_sampled_when_known(self):
        snapshot = assemble(all_results(), None, now=NOW)
        snapshot["flow"]["reworkLast24h"] = 2
        self.assertEqual(snapshot_samples(snapshot)["flow.rework24h"], 2.0)


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
            first = collector.run_once(config, now=NOW, collect=lambda *_: all_results())
            self.assertEqual(len(first["machine"]["history"]), 1)
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

    def test_meta_collector_counts_loops_and_source_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                jira_site="https://x.example", jira_user="u", jira_token="t",
                github_repos=("a/b",), data_dir=Path(tmp), interval_seconds=45,
            )
            first = collector.run_once(
                config, now=NOW, collect=lambda *_: all_results(), version="abc1234"
            )["meta"]["collector"]
            self.assertEqual(first["version"], "abc1234")
            self.assertEqual(first["loopCount"], 1)
            self.assertGreaterEqual(first["lastLoopSeconds"], 0)
            self.assertEqual(first["sourceErrors"],
                             {"jira": 0, "github": 0, "machine": 0, "events": 0})

            failing = {**all_results(), "github": OSError("x")}
            for step in (1, 2):
                second = collector.run_once(
                    config, now=NOW + dt.timedelta(seconds=45 * step),
                    collect=lambda *_: failing, version="abc1234",
                )["meta"]["collector"]
            self.assertEqual(second["loopCount"], 3)
            self.assertEqual(second["sourceErrors"]["github"], 2)
            self.assertEqual(second["sourceErrors"]["jira"], 0)

    def test_git_head_is_short_hash_or_none(self):
        head = collector.git_head()
        self.assertTrue(head is None or 4 <= len(head) <= 40)

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
