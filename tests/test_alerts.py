import datetime as dt
import unittest

from controlroom.alerts import compute_alerts

NOW = dt.datetime(2026, 10, 21, 12, 0, 0, tzinfo=dt.timezone.utc)


def at(seconds_ago):
    return (NOW - dt.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def snapshot(**parts):
    base = {
        "meta": {"asOf": at(30)},
        "machine": {"heartbeatAges": {"hb:w1": 5}},
        "ai": {"agents": [{"id": "codex", "name": "Codex", "usable": True}]},
        "flow": {"inflight": [{"key": "JOOCR-1", "status": "In QA", "statusSince": at(60)}]},
        "projects": {"list": [{"key": "JOOCR", "openPRs": [{"number": 8, "title": "feat: x (JOOCR-1)"}]}]},
    }
    base.update(parts)
    return base


class AlertRulesTest(unittest.TestCase):
    def rules(self, snap):
        return [(a["rule"], a["subject"]) for a in compute_alerts(snap, NOW)]

    def test_healthy_data_has_no_alert(self):
        self.assertEqual(compute_alerts(snapshot(), NOW), [])

    def test_empty_snapshot_has_no_alert(self):
        self.assertEqual(compute_alerts({}, NOW), [])

    def test_heartbeat_boundary(self):
        self.assertEqual(self.rules(snapshot(machine={"heartbeatAges": {"hb:w1": 180}})), [])
        self.assertEqual(self.rules(snapshot(machine={"heartbeatAges": {"hb:w1": 181}})), [("heartbeat", "w1")])

    def test_ai_out_of_pool(self):
        agents = [{"id": "grok", "name": "Grok", "usable": False}, {"id": "codex", "name": "Codex", "usable": True}]
        alerts = compute_alerts(snapshot(ai={"agents": agents}), NOW)
        self.assertEqual([(a["rule"], a["subject"]) for a in alerts], [("ai", "Grok")])
        self.assertEqual(alerts[0]["link"], {"type": "ai", "key": "grok"})

    def test_pull_request_waiting_for_ci_boundary(self):
        def flow(seconds, status="In QA"):
            return {"inflight": [{"key": "JOOCR-1", "status": status, "statusSince": at(seconds)}]}

        self.assertEqual(self.rules(snapshot(flow=flow(90 * 60))), [])
        self.assertEqual(self.rules(snapshot(flow=flow(90 * 60 + 1))), [("ci", "JOOCR-1")])
        self.assertEqual(self.rules(snapshot(flow=flow(5 * 3600, "In Review"))), [])

    def test_qa_without_an_open_pull_request_is_not_a_ci_wait(self):
        flow = {"inflight": [{"key": "JOOCR-9", "status": "In QA", "statusSince": at(5 * 3600)}]}
        self.assertEqual(self.rules(snapshot(flow=flow)), [])

    def test_blocked_boundary(self):
        def flow(seconds):
            return {"inflight": [{"key": "JOOCR-2", "status": "Blocked", "statusSince": at(seconds)}]}

        self.assertEqual(self.rules(snapshot(flow=flow(24 * 3600))), [])
        alerts = compute_alerts(snapshot(flow=flow(24 * 3600 + 1)), NOW)
        self.assertEqual([(a["rule"], a["subject"]) for a in alerts], [("blocked", "JOOCR-2")])
        self.assertEqual(alerts[0]["link"], {"type": "ticket", "key": "JOOCR-2"})

    def test_collector_data_age_boundary(self):
        self.assertEqual(self.rules(snapshot(meta={"asOf": at(15 * 60)})), [])
        self.assertEqual(self.rules(snapshot(meta={"asOf": at(15 * 60 + 1)})), [("collector", "collector")])

    def test_ticket_without_a_usable_time_is_ignored(self):
        flow = {"inflight": [{"key": "JOOCR-3", "status": "Blocked", "statusSince": None}]}
        self.assertEqual(self.rules(snapshot(flow=flow)), [])


class AssemblerAlertsTest(unittest.TestCase):
    def test_snapshot_carries_the_alerts_and_hides_them_when_healthy(self):
        from controlroom.assembler import assemble

        snap = assemble({}, None, now=NOW)  # every source failed: no asOf yet, nothing to alert about
        self.assertEqual(snap["alerts"], {"list": []})
        older = {"meta": {"asOf": at(20 * 60)}}
        again = assemble({}, {**snap, **older}, now=NOW)
        self.assertEqual([a["rule"] for a in again["alerts"]["list"]], ["collector"])


class StatusSinceTest(unittest.TestCase):
    def test_uses_the_latest_move_into_the_status_not_the_last_edit(self):
        from controlroom.sources.jira import _status_since

        def move(at_, to):
            return {"created": at_, "items": [{"field": "status", "toString": to}]}

        histories = [
            move("2026-10-19T08:00:00.000+0000", "Blocked"),
            move("2026-10-19T09:00:00.000+0000", "To Do"),
            move("2026-10-19T10:00:00.000+0000", "Blocked"),
            {"created": "2026-10-20T10:00:00.000+0000", "items": [{"field": "labels", "toString": "x"}]},
        ]
        self.assertEqual(_status_since("Blocked", histories), "2026-10-19T10:00:00Z")
        self.assertIsNone(_status_since("In QA", histories))


if __name__ == "__main__":
    unittest.main()
