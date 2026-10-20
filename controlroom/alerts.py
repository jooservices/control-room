"""Alert rules: what needs the owner's attention, computed from a finished snapshot.

``compute_alerts`` is pure. Alerts are display only: nothing is sent anywhere.
Waiting times run from the moment the ticket entered the status (``statusSince``, from the Jira changelog), not from its last edit.
Each alert has rule, subject, text and a ``link`` object (type ticket or ai, plus key)
that the page uses to open the drawer.
"""

import datetime as dt
import re

HEARTBEAT_MAX_SECONDS = 3 * 60
CI_WAIT_MAX_SECONDS = 90 * 60
BLOCKED_MAX_SECONDS = 24 * 3600
COLLECTOR_MAX_SECONDS = 15 * 60


def _parse(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _age(value, now):
    moment = _parse(value)
    return None if moment is None else (now - moment).total_seconds()


def _minutes(seconds):
    return int(seconds // 60)


def compute_alerts(snapshot, now):
    """Return the alert rows for ``snapshot`` at ``now`` (an aware datetime), most urgent rule first."""
    alerts = []

    age = _age((snapshot.get("meta") or {}).get("asOf"), now)
    if age is not None and age > COLLECTOR_MAX_SECONDS:
        alerts.append({
            "rule": "collector", "subject": "collector",
            "text": f"Collector data is {_minutes(age)} minutes old",
            "link": {"type": "project", "key": "JOOCR"},
        })

    for name, seconds in sorted(((snapshot.get("machine") or {}).get("heartbeatAges") or {}).items()):
        if isinstance(seconds, (int, float)) and seconds > HEARTBEAT_MAX_SECONDS:
            alerts.append({
                "rule": "heartbeat", "subject": name.replace("hb:", ""),
                "text": f"Worker {name.replace('hb:', '')} has not sent a heartbeat for {_minutes(seconds)} minutes",
                "link": {"type": "project", "key": "JOOCR"},
            })

    for agent in (snapshot.get("ai") or {}).get("agents") or []:
        if agent.get("usable") is False:
            alerts.append({
                "rule": "ai", "subject": agent.get("name") or agent.get("id"),
                "text": f"{agent.get('name') or agent.get('id')} is out of the pool",
                "link": {"type": "ai", "key": agent.get("id")},
            })

    open_pr_titles = [
        pr.get("title") or ""
        for project in (snapshot.get("projects") or {}).get("list") or []
        for pr in project.get("openPRs") or []
    ]
    for ticket in (snapshot.get("flow") or {}).get("inflight") or []:
        waited = _age(ticket.get("statusSince"), now)
        if waited is None:
            continue
        has_open_pr = any(re.search(rf"(?<![\w-]){re.escape(ticket['key'])}(?!\w)", title) for title in open_pr_titles)
        if ticket.get("status") == "In QA" and has_open_pr and waited > CI_WAIT_MAX_SECONDS:
            alerts.append({
                "rule": "ci", "subject": ticket["key"],
                "text": f"{ticket['key']} has had its pull request in QA for {_minutes(waited)} minutes",
                "link": {"type": "ticket", "key": ticket["key"]},
            })
        if ticket.get("status") == "Blocked" and waited > BLOCKED_MAX_SECONDS:
            alerts.append({
                "rule": "blocked", "subject": ticket["key"],
                "text": f"{ticket['key']} has been Blocked for {int(waited // 3600)} hours",
                "link": {"type": "ticket", "key": ticket["key"]},
            })
    return alerts
