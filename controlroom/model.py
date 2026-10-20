"""Versioned plain-dict snapshot shape used by the prototype page.

The root has ``version`` plus seven documents. ``projects.list`` contains rows
with key, name, repo, board, total, counts, epics, active, recent, doneLast24h,
mergedPRs, openPRs, lastUpdate, onFlow, eta, etaSamples, etaExcluded, and
etaCycleCache. The cache maps completed ticket keys to observed cycle minutes
or null when Jira history has no usable cycle; it avoids refetching old history.
Flow project ETA values contain low, middle, and high minutes; non-flow projects
have no ETA. Epic rows have key, summary, status, children, done, eta,
etaSamples, and etaExcluded; active ticket rows have key, type, status, priority,
summary, parent, and updated; recent rows have key, summary, and closed; merged
pull request rows have number, title, mergedAt, and url; open pull requests have
number, title, url, and draft.

``flow`` contains watched, inflight, statusCounts, cycle (n/p25/p50/p75),
doneLast24h, doneByAiLast24h, reworkLast24h, mergedPRs24h, qaslot, and paused.
Inflight ticket rows have key, project, summary, status, priority, labels,
agent, updated, lease, pr, devAi, and reviewerAi; a linked ``pr`` has number,
url, state, and draft. ``ai`` contains agents, versions, and usable; each agent
has id, name, model, effort, roles, probe, usable, cooling, runs24h, minutes24h,
bad24h, runsAll, minutesAll, and badAll. ``ai.usage`` rows come from the
history store's run log (each run stored once by usage-log key) and have ai, role,
and runs, minutes and bad (runs that did not end ok) for each window: the keys
end in 24h, 7d or 30d (for example runs7d). ``timeline`` contains runs, merges,
and now. Run rows have key, role, ai, start, end, minutes, and ok; merge rows
have key, number, title, at, url, and project. ``events.list`` rows have at,
kind, key, project, and text, plus a ``link`` object with ``type`` (ticket,
project, or ai) and ``key``; merge events also have the source pull request
``url``. Events are newest first and capped at 200.

``machine`` contains host, cpus, load, mem, disk, uptimeH, containers, services,
heartbeatAges, redis, code, leases, workersBusy, workersTotal, history, and runs. History rows
have t, load, and mem; CI run rows have name, url, status, and mins. ``meta``
contains asOf, ok, and flowProjects. Existing keys match
``docs/prototype/collect.py`` and the prototype page; ETA fields extend the
project and epic rows.

The empty values are schema defaults for a collector to populate; they do not
represent a successful source read. Flow numbers no source collects (cycle,
doneByAiLast24h, qaslot, paused) default to ``None``: unknown. ``reworkLast24h``
is derived from Jira status events by the assembler and stays ``None`` until
that source has been read successfully.
"""

SNAPSHOT_VERSION = 1
DOCUMENT_NAMES = ("projects", "flow", "ai", "timeline", "events", "machine", "meta")

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
ETA_EXCLUDED_STATUSES = ("Blocked", "Human review", "Ready to merge")
HOLD_LABEL = "aiflow-hold"


def empty_snapshot() -> dict[str, object]:
    """Return a fresh snapshot whose document values are plain dictionaries."""
    return {
        "version": SNAPSHOT_VERSION,
        "projects": {"list": []},
        "flow": {
            "watched": [],
            "inflight": [],
            "statusCounts": {},
            "cycle": {"n": None, "p25": None, "p50": None, "p75": None},
            "doneLast24h": 0,
            "doneByAiLast24h": None,
            "reworkLast24h": None,
            "mergedPRs24h": 0,
            "qaslot": None,
            "paused": None,
        },
        "ai": {"agents": [], "versions": {}, "usable": {}, "usage": []},
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
            "workersTotal": None,
            "history": [],
            "runs": [],
        },
        "meta": {"asOf": None, "ok": {}, "flowProjects": []},
    }
