"""Versioned plain-dict snapshot shape used by the prototype page.

The root has ``version`` plus seven documents. ``projects.list`` contains rows
with key, name, repo, board, total, counts, epics, active, recent, doneLast24h,
mergedPRs, openPRs, lastUpdate, and onFlow. Epic rows have key, summary, status,
children, and done; active ticket rows have key, type, status, priority,
summary, parent, and updated; recent rows have key, summary, and closed; merged
pull request rows have number, title, mergedAt, and url; open pull requests have
number, title, url, and draft.

``flow`` contains watched, inflight, statusCounts, cycle (n/p25/p50/p75),
doneLast24h, doneByAiLast24h, reworkLast24h, mergedPRs24h, qaslot, and paused.
Inflight ticket rows have key, project, summary, status, priority, labels,
agent, updated, lease, pr, devAi, and reviewerAi; a linked ``pr`` has number,
url, state, and draft. ``ai`` contains agents, versions, and usable; each agent
has id, name, model, effort, roles, probe, usable, cooling, runs24h, minutes24h,
bad24h, runsAll, minutesAll, and badAll. ``timeline`` contains runs, merges,
and now. Run rows have key, role, ai, start, end, minutes, and ok; merge rows
have key, number, title, at, url, and project. ``events.list`` rows have at,
kind, key, and text, with url on merge events.

``machine`` contains host, cpus, load, mem, disk, uptimeH, containers, services,
heartbeatAges, redis, code, leases, workersBusy, history, and runs. History rows
have t, load, and mem; CI run rows have name, url, status, and mins. ``meta``
contains asOf, ok, and flowProjects. These keys match
``docs/prototype/collect.py`` and the prototype page.

The empty values are schema defaults for a collector to populate; they do not
represent a successful source read.
"""

SNAPSHOT_VERSION = 1
DOCUMENT_NAMES = ("projects", "flow", "ai", "timeline", "events", "machine", "meta")


def empty_snapshot() -> dict[str, object]:
    """Return a fresh snapshot whose document values are plain dictionaries."""
    return {
        "version": SNAPSHOT_VERSION,
        "projects": {"list": []},
        "flow": {
            "watched": [],
            "inflight": [],
            "statusCounts": {},
            "cycle": {"n": 0, "p25": None, "p50": None, "p75": None},
            "doneLast24h": 0,
            "doneByAiLast24h": 0,
            "reworkLast24h": 0,
            "mergedPRs24h": 0,
            "qaslot": {},
            "paused": False,
        },
        "ai": {"agents": [], "versions": {}, "usable": {}},
        "timeline": {"runs": [], "merges": [], "now": 0},
        "events": {"list": []},
        "machine": {
            "host": "",
            "cpus": None,
            "load": None,
            "mem": None,
            "disk": None,
            "uptimeH": None,
            "containers": None,
            "services": {},
            "heartbeatAges": {},
            "redis": None,
            "code": None,
            "leases": {},
            "workersBusy": 0,
            "history": [],
            "runs": [],
        },
        "meta": {"asOf": None, "ok": {}, "flowProjects": []},
    }
