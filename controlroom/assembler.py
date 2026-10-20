"""Assemble one consistent snapshot from the source results.

``assemble`` is pure: it takes what each source returned (or the exception it
raised) plus the previous snapshot and returns the next snapshot. A failed
source keeps the previous values it owns; the other sources still update.
"""

import copy
import datetime as dt
from typing import Mapping

from controlroom.model import empty_snapshot

SOURCES = ("jira", "github", "machine")

# Statuses that count as in flight (same list as the prototype collector).
FLOW_STATUSES = (
    "To Do",
    "In Progress",
    "In Review",
    "Review passed",
    "In QA",
    "Human review",
    "Ready to merge",
    "Blocked",
)
MAX_EVENTS = 80

# AI agents shown on the page: id -> (name, model, effort, roles). Same table
# as the prototype collector.
AGENTS = {
    "codex": ("Codex", "gpt-6-luna", "xhigh", "developer (default)"),
    "claude": (
        "Claude",
        "Sonnet",
        "default",
        "QA (default); developer for Highest priority",
    ),
    "grok": ("Grok", "grok-build", "default", "reviewer (default)"),
    "opencode": (
        "OpenCode",
        "DeepSeek V4.1 Flash (free)",
        "default",
        "only when a ticket names it",
    ),
}

# Event kinds, by the source that produces them.
_EVENT_KINDS = {
    "jira": {"status"},
    "machine": {"run", "badrun"},
    "github": {"merge"},
}


def iso(value: dt.datetime) -> str:
    """Format an aware datetime as a UTC ``YYYY-MM-DDTHH:MM:SSZ`` string."""
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_failure(name: str, result: object) -> str | None:
    """Return a short reason when ``result`` is not a usable source result."""
    if isinstance(result, BaseException):
        return type(result).__name__
    if name == "github" and isinstance(result, Mapping) and result.get("error"):
        return str(result["error"])
    return None


def assemble(
    results: Mapping[str, object],
    previous: Mapping[str, object] | None,
    *,
    now: dt.datetime,
    machine_history: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Return the next snapshot.

    ``results`` maps a source name to its result, or to the exception it
    raised. Expected results: ``jira`` is ``(projects_document, status_events)``,
    ``github`` is the dict of ``sources.github.collect`` and ``machine`` is the
    dict of ``sources.machine.collect``. Sources missing from ``results`` count
    as failed. ``machine_history`` replaces ``machine.history`` when given.

    ``meta.ok[source]`` is false for a failed source and ``meta.errorAt[source]``
    holds the time of its latest failure. ``meta.asOf`` moves only when at least
    one source succeeded.
    """
    snapshot = empty_snapshot()
    if previous:
        for name, value in previous.items():
            if name != "version" and name in snapshot:
                snapshot[name] = copy.deepcopy(value)
    old_meta = snapshot["meta"] if isinstance(snapshot["meta"], dict) else {}
    now_iso = iso(now)
    epoch = int(now.timestamp())

    good: dict[str, object] = {}
    ok: dict[str, bool] = {}
    error_at = dict(old_meta.get("errorAt") or {})
    for name in SOURCES:
        result = results.get(name, RuntimeError("missing"))
        failure = source_failure(name, result)
        ok[name] = failure is None
        if failure is None:
            good[name] = result
            error_at.pop(name, None)
        else:
            error_at[name] = now_iso

    flow_projects = list(old_meta.get("flowProjects") or [])
    if "machine" in good:
        flow_projects = list(good["machine"].get("flowProjects") or [])

    _apply_projects(snapshot, good)
    _apply_flow(snapshot, good, flow_projects, now)
    _apply_inflight(snapshot, good, flow_projects)
    _apply_done_by_ai(snapshot, good, now)
    if "machine" in good:
        _apply_ai(snapshot, good["machine"])
    _apply_machine(snapshot, good, machine_history)
    _apply_timeline(snapshot, good, flow_projects, now, epoch)
    _apply_events(snapshot, good, now)

    snapshot["meta"] = {
        "asOf": now_iso if good else old_meta.get("asOf"),
        "ok": ok,
        "errorAt": error_at,
        "flowProjects": flow_projects,
    }
    return snapshot


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _within(value: object, now: dt.datetime, seconds: int) -> bool:
    moment = _parse_time(value)
    return moment is not None and 0 <= (now - moment).total_seconds() < seconds


def _apply_projects(snapshot, good) -> None:
    rows = snapshot["projects"]["list"]
    if "jira" in good:
        previous = {row["key"]: row for row in rows}
        rows = copy.deepcopy(good["jira"][0]["list"])
        for row in rows:
            # The PR lists come from GitHub; keep the old ones if it is down.
            old = previous.get(row["key"], {})
            row["mergedPRs"] = old.get("mergedPRs", [])
            row["openPRs"] = old.get("openPRs", [])
    if "github" in good:
        by_project = good["github"]["projects"]
        for row in rows:
            data = by_project.get(row["key"])
            if data is not None:
                row["mergedPRs"] = [
                    {**pr, "title": pr["title"][:90]} for pr in data["mergedPRs"]
                ][:4]
                row["openPRs"] = [
                    {**pr, "title": pr["title"][:80]} for pr in data["openPRs"]
                ][:5]
    snapshot["projects"] = {"list": rows}


def _apply_flow(snapshot, good, flow_projects, now) -> None:
    flow = snapshot["flow"]
    if "machine" in good:
        flow["watched"] = list(flow_projects)
    if "jira" in good:
        counts: dict[str, int] = {}
        done = 0
        for row in good["jira"][0]["list"]:
            if row["key"] not in flow_projects:
                continue
            done += row["doneLast24h"]
            for status, number in row["counts"].items():
                counts[status] = counts.get(status, 0) + number
            for epic in row["epics"]:
                counts[epic["status"]] = counts.get(epic["status"], 0) - 1
        flow["statusCounts"] = {k: v for k, v in sorted(counts.items()) if v > 0}
        flow["doneLast24h"] = done
    if "github" in good:
        flow["mergedPRs24h"] = sum(
            1
            for key in flow_projects
            for pr in good["github"]["projects"].get(key, {}).get("mergedPRs", [])
            if _within(pr.get("mergedAt"), now, 86400)
        )


def _apply_inflight(snapshot, good, flow_projects) -> None:
    """Fill ``flow.inflight`` from Jira active rows and machine leases.

    Fields no source collects (labels, agent, PR link, role AIs) are ``None``
    rather than invented values.
    """
    flow = snapshot["flow"]
    leases = None
    if "machine" in good:
        leases = good["machine"].get("leases") or {}
    if "jira" in good:
        leases = leases if leases is not None else snapshot["machine"].get("leases") or {}
        rows = []
        for project in good["jira"][0]["list"]:
            if project["key"] not in flow_projects:
                continue
            for ticket in project["active"]:
                if ticket["status"] not in FLOW_STATUSES:
                    continue
                rows.append(
                    {
                        "key": ticket["key"],
                        "project": project["key"],
                        "summary": ticket["summary"],
                        "status": ticket["status"],
                        "priority": ticket["priority"],
                        "labels": None,
                        "agent": None,
                        "updated": ticket["updated"],
                        "lease": leases.get(ticket["key"]),
                        "pr": None,
                        "devAi": None,
                        "reviewerAi": None,
                    }
                )
        flow["inflight"] = rows
    elif leases is not None and isinstance(flow.get("inflight"), list):
        for row in flow["inflight"]:
            row["lease"] = leases.get(row["key"])


def _apply_done_by_ai(snapshot, good, now) -> None:
    """Count tickets moved to Done in 24 hours that an AI worked on.

    Needs both Jira status events and machine usage; when either source is
    down the previous value stays.
    """
    if "jira" not in good or "machine" not in good:
        return
    base = good["machine"].get("now", int(now.timestamp()))
    worked = {
        run.get("key")
        for run in good["machine"].get("usage") or []
        if run.get("key") and base - run["end"] < 86400
    }
    done = {
        event["key"]
        for event in good["jira"][1]
        if event.get("kind") == "status"
        and event["text"].rsplit("→", 1)[-1].strip() == "Done"
        and _within(event.get("at"), now, 86400)
    }
    snapshot["flow"]["doneByAiLast24h"] = len(done & worked)


def _minutes(rows) -> float:
    return round(sum(run.get("minutes", 0) for run in rows), 1)


def _bad(rows) -> int:
    return sum(1 for run in rows if run.get("why") != "ok")


def _apply_ai(snapshot, raw) -> None:
    usage = raw.get("usage") or []
    probes = raw.get("probe") or {}
    cooldown = raw.get("cooldown") or {}
    base = raw.get("now", 0)
    agents = []
    for agent_id, (name, model, effort, roles) in AGENTS.items():
        runs = [run for run in usage if run.get("ai") == name]
        last_day = [run for run in runs if base - run["end"] < 86400]
        probe = probes.get("wsl:" + name) or ""
        agents.append(
            {
                "id": agent_id,
                "name": name,
                "model": model,
                "effort": effort,
                "roles": roles,
                "probe": probe or "not probed yet",
                "usable": not probe.startswith("down"),
                "cooling": name in cooldown,
                "runs24h": len(last_day),
                "minutes24h": _minutes(last_day),
                "bad24h": _bad(last_day),
                "runsAll": len(runs),
                "minutesAll": _minutes(runs),
                "badAll": _bad(runs),
            }
        )
    snapshot["ai"] = {"agents": agents, "versions": {}, "usable": {}}


def _apply_machine(snapshot, good, machine_history) -> None:
    machine = snapshot["machine"]
    if "machine" in good:
        raw = good["machine"]
        leases = raw.get("leases") or {}
        machine.update(
            {
                "host": raw.get("host", ""),
                "cpus": raw.get("cpus"),
                "load": raw.get("load"),
                "mem": raw.get("mem"),
                "disk": raw.get("disk"),
                "uptimeH": raw.get("uptime_h"),
                "containers": raw.get("containers"),
                "services": raw.get("services") or {},
                "heartbeatAges": raw.get("hb") or {},
                "redis": raw.get("redis"),
                "code": raw.get("code"),
                "leases": leases,
                "workersBusy": len(leases),
            }
        )
    if machine_history is not None:
        machine["history"] = machine_history
    if "github" in good:
        runs = []
        for data in good["github"]["projects"].values():
            runs.extend(data["ciRuns"])
        machine["runs"] = runs[:8]


def _apply_timeline(snapshot, good, flow_projects, now, epoch) -> None:
    timeline = snapshot["timeline"]
    if "machine" in good:
        base = good["machine"].get("now", epoch)
        runs = [
            {
                "key": run.get("key"),
                "role": run.get("role"),
                "ai": run.get("ai"),
                "start": run["end"] - int(run.get("minutes", 0) * 60),
                "end": run["end"],
                "minutes": run.get("minutes", 0),
                "ok": run.get("why") == "ok",
            }
            for run in good["machine"].get("usage") or []
            if base - run["end"] < 6 * 3600
        ]
        timeline["runs"] = sorted(runs, key=lambda run: run["start"])
    if "github" in good:
        merges = []
        for key in flow_projects:
            data = good["github"]["projects"].get(key, {})
            for pr in data.get("mergedPRs", []):
                if _within(pr.get("mergedAt"), now, 6 * 3600):
                    merges.append(
                        {
                            "key": _ticket_key(pr["title"], key),
                            "number": pr["number"],
                            "title": pr["title"][:80],
                            "at": pr["mergedAt"],
                            "url": pr["url"],
                            "project": key,
                        }
                    )
        timeline["merges"] = merges
    timeline["now"] = epoch


def _ticket_key(title: str, project: str) -> str | None:
    for part in title.split("(")[1:]:
        candidate = part.split(")")[0]
        if candidate.startswith(project + "-"):
            return candidate
    return None


def _apply_events(snapshot, good, now) -> None:
    refreshed = set()
    for source in good:
        refreshed |= _EVENT_KINDS[source]
    events = [
        event
        for event in snapshot["events"]["list"]
        if event.get("kind") not in refreshed
    ]
    if "jira" in good:
        events.extend(good["jira"][1])
    if "machine" in good:
        base = good["machine"].get("now", int(now.timestamp()))
        for run in good["machine"].get("usage") or []:
            if base - run["end"] >= 86400:
                continue
            finished = run.get("why") == "ok"
            text = "{} finished a {} step in {} min".format(
                run.get("ai"), run.get("role"), run.get("minutes")
            )
            if not finished:
                text += " (ended: {})".format(run.get("why"))
            events.append(
                {
                    "at": iso(dt.datetime.fromtimestamp(run["end"], dt.timezone.utc)),
                    "kind": "run" if finished else "badrun",
                    "key": run.get("key"),
                    "text": text,
                }
            )
    if "github" in good:
        for data in good["github"]["projects"].values():
            for pr in data.get("mergedPRs", []):
                if _within(pr.get("mergedAt"), now, 86400):
                    events.append(
                        {
                            "at": pr["mergedAt"],
                            "kind": "merge",
                            "key": None,
                            "text": "{} PR #{} merged: {}".format(
                                data["repo"].split("/")[-1],
                                pr["number"],
                                pr["title"][:70],
                            ),
                            "url": pr["url"],
                        }
                    )
    events.sort(key=lambda event: event["at"], reverse=True)
    snapshot["events"] = {"list": events[:MAX_EVENTS]}
