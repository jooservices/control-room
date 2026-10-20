import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from controlroom.sources import machine
from controlroom.sources.machine import (
    _read_redis_state,
    parse_loadavg,
    parse_meminfo,
    parse_proc_metrics,
    parse_systemd_output,
    parse_uptime,
    parse_usage_log,
    parse_watched_projects,
)


FIXTURES = Path(__file__).parent / "fixtures" / "machine"
NOW = 2_000_000_000


class MachineParserTest(unittest.TestCase):
    def test_parses_proc_files_from_fixtures(self):
        metrics = parse_proc_metrics(FIXTURES / "proc")

        self.assertEqual(metrics["load"], [0.12, 0.34, 0.56])
        self.assertEqual(metrics["mem"], {"total": 16.0, "avail": 8.0})
        self.assertEqual(metrics["uptime_h"], 48.0)

    def test_proc_parsers_reject_missing_fields(self):
        with self.assertRaises(ValueError):
            parse_loadavg("0.1 0.2")
        with self.assertRaises(ValueError):
            parse_meminfo("MemTotal: 1024 kB")
        with self.assertRaises(ValueError):
            parse_uptime("")

    def test_parses_only_valid_usage_runs_from_the_last_day(self):
        fixture = json.loads(
            (FIXTURES / "usage-log.json").read_text(encoding="utf-8")
        )

        runs = parse_usage_log(fixture, now=NOW)

        self.assertEqual(
            [run["end"] for run in runs], [1_999_913_600, 1_999_999_999]
        )
        self.assertEqual([run["ai"] for run in runs], ["Claude", "Codex"])
        self.assertEqual(runs[-1]["name"], "usage:JAIF-12:Codex:1999999999")

    def test_parses_systemd_unit_states_from_fixture(self):
        output = (FIXTURES / "systemd-units.txt").read_text(encoding="utf-8")

        self.assertEqual(
            parse_systemd_output(output),
            {
                "aiflow-work@1.service": "active",
                "aiflow-work@2.service": "inactive",
                "aiflow-health.timer": "active",
            },
        )

    def test_discovers_project_keys_from_config_entries(self):
        output = (FIXTURES / "supervisor-check.txt").read_text(encoding="utf-8")

        self.assertEqual(parse_watched_projects(output), ["JAIF", "JOOCR", "JAVM"])

    def test_redis_state_uses_only_get_and_scan_reads(self):
        class FakeRedis:
            values = {
                "hb:worker-1": str(NOW - 30),
                "lease:JAIF-12": "Codex",
                "probe:wsl:Codex": "ok",
                "usage:JAIF-12:Codex:1999999999": (
                    '{"ai":"Codex","minutes":3,"why":"ok"}'
                ),
                "usage:JAIF-8:Codex:1999913599": (
                    '{"ai":"Codex","minutes":1,"why":"ok"}'
                ),
            }

            def __init__(self):
                self.operations = []

            def scan(self, pattern):
                self.operations.append("SCAN")
                prefix = pattern[:-1]
                keys = [key for key in self.values if key.startswith(prefix)]
                if pattern.startswith("cooldown:"):
                    keys.append("cooldown:Codex")
                return keys

            def get(self, key):
                self.operations.append("GET")
                return self.values.get(key)

        redis = FakeRedis()

        state = _read_redis_state(redis, NOW)

        self.assertEqual(state["hb"], {"hb:worker-1": 30})
        self.assertEqual(state["leases"], {"JAIF-12": "Codex"})
        self.assertEqual(state["probe"], {"wsl:Codex": "ok"})
        self.assertEqual(state["cooldown"], {"Codex": True})
        self.assertEqual(len(state["usage"]), 1)
        self.assertTrue(set(redis.operations).issubset({"GET", "SCAN"}))

    @patch("controlroom.sources.machine.os.statvfs")
    @patch("controlroom.sources.machine._run_command")
    def test_collect_combines_local_metrics_and_discovered_flow_state(
        self, run_command, statvfs
    ):
        class FakeRedis:
            values = {
                "hb:worker-1": str(NOW - 10),
                "lease:JAIF-12": "Codex",
                "probe:wsl:Codex": "ok",
                "cooldown:Claude": None,
                "usage:JAIF-12:Codex:1999999999": (
                    '{"ai":"Codex","minutes":3,"why":"ok"}'
                ),
            }

            def scan(self, pattern):
                return [
                    key for key in self.values if key.startswith(pattern[:-1])
                ]

            def get(self, key):
                return self.values[key]

        systemd_output = (FIXTURES / "systemd-units.txt").read_text(
            encoding="utf-8"
        )
        config_output = (FIXTURES / "supervisor-check.txt").read_text(
            encoding="utf-8"
        )

        def command_output(args, **_kwargs):
            if args[0] == "docker":
                return "workspaces-shared-redis-1\nunrelated-container\n"
            if args[0] == "systemctl":
                return systemd_output
            if args[0] == "git":
                return "abc123\n"
            if args[1:] == ["-m", "aiflow.supervisor", "check"]:
                return config_output
            raise AssertionError(f"Unexpected local command: {args[0]}")

        run_command.side_effect = command_output
        statvfs.return_value = SimpleNamespace(
            f_blocks=1500, f_frsize=1_000_000, f_bfree=500
        )

        state = machine.collect(
            now=NOW,
            proc_root=FIXTURES / "proc",
            flow_root="/fake/ai-flow",
            redis_client=FakeRedis(),
        )

        self.assertEqual(state["hb"], {"hb:worker-1": 10})
        self.assertEqual(state["leases"], {"JAIF-12": "Codex"})
        self.assertEqual(state["probe"], {"wsl:Codex": "ok"})
        self.assertEqual(state["cooldown"], {"Claude": True})
        self.assertEqual(len(state["usage"]), 1)
        self.assertEqual(state["load"], [0.12, 0.34, 0.56])
        self.assertEqual(state["mem"], {"total": 16.0, "avail": 8.0})
        self.assertEqual(state["disk"], {"total": 2, "used": 1})
        self.assertEqual(state["containers"], 1)
        self.assertEqual(state["services"]["aiflow-work@1.service"], "active")
        self.assertEqual(state["code"], "abc123")
        self.assertEqual(state["host"], "Workstation")
        self.assertEqual(state["flowProjects"], ["JAIF", "JOOCR", "JAVM"])
        commands = [call.args[0][0] for call in run_command.call_args_list]
        self.assertNotIn("ssh", commands)


if __name__ == "__main__":
    unittest.main()
