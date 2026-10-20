"""SQLite history of the numbers the page charts and the ETA need."""

import contextlib
import datetime as dt
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

from controlroom.assembler import AGENTS

RETENTION_DAYS = 90
FIXED_METRICS = frozenset(
    {"machine.load", "machine.mem_used", "flow.done24h", "flow.rework24h"}
)
USAGE_WINDOWS = (("24h", 1), ("7d", 7), ("30d", 30))
AI_MINUTES_METRIC = re.compile(r"ai\.([A-Za-z0-9_-]+)\.minutes24h")


def is_known_metric(metric: str) -> bool:
    """Tell whether ``metric`` is one of the series the history API serves."""
    if metric in FIXED_METRICS:
        return True
    match = AI_MINUTES_METRIC.fullmatch(metric)
    return match is not None and match.group(1) in AGENTS


def snapshot_samples(snapshot: Mapping[str, object]) -> dict[str, float]:
    """Return metric -> value for the numbers worth charting.

    Only sources that succeeded in this round are sampled, so a stale document
    is never recorded again as if it were new.
    """
    meta = snapshot["meta"]
    ok = meta.get("ok", {})
    samples: dict[str, float] = {}
    if ok.get("machine"):
        machine = snapshot["machine"]
        load = machine.get("load")
        if load:
            samples["machine.load"] = float(load[0])
        mem = machine.get("mem")
        if mem:
            samples["machine.mem_used"] = round(mem["total"] - mem["avail"], 1)
        samples["machine.workers_busy"] = float(machine.get("workersBusy", 0))
        for agent in snapshot["ai"]["agents"]:
            samples["ai.{}.runs24h".format(agent["id"])] = float(agent["runs24h"])
            minutes = agent.get("minutes24h")
            if minutes is not None:
                samples["ai.{}.minutes24h".format(agent["id"])] = float(minutes)
    if ok.get("jira"):
        flow = snapshot["flow"]
        samples["flow.done24h"] = float(flow["doneLast24h"])
        if flow.get("reworkLast24h") is not None:
            samples["flow.rework24h"] = float(flow["reworkLast24h"])
        for status, number in flow["statusCounts"].items():
            samples["flow.status.{}".format(status)] = float(number)
        for row in snapshot["projects"]["list"]:
            samples["project.{}.total".format(row["key"])] = float(row["total"])
            samples["project.{}.done".format(row["key"])] = float(
                row["counts"].get("Done", 0)
            )
    return samples


class HistoryStore:
    """Append-only samples ``(ts, metric, value)`` in one SQLite file."""

    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS samples ("
                "ts INTEGER NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS samples_metric_ts ON samples (metric, ts)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "run_id TEXT PRIMARY KEY, ended INTEGER NOT NULL, ai TEXT NOT NULL, "
                "role TEXT NOT NULL, minutes REAL NOT NULL, ok INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS runs_ended ON runs (ended)"
            )

    @contextlib.contextmanager
    def _connect(self):
        """Yield a connection that commits on success and always closes."""
        if self.read_only:
            connection = sqlite3.connect(
                "{}?mode=ro".format(self.path.resolve().as_uri()), uri=True
            )
        else:
            connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def insert(self, ts: int, samples: Mapping[str, float]) -> None:
        """Append one row per metric, all stamped with ``ts`` (unix seconds)."""
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO samples (ts, metric, value) VALUES (?, ?, ?)",
                [(int(ts), metric, value) for metric, value in samples.items()],
            )

    def prune(self, now: dt.datetime, days: int = RETENTION_DAYS) -> int:
        """Delete samples older than ``days``; return how many were removed."""
        cutoff = int((now - dt.timedelta(days=days)).timestamp())
        with self._connect() as connection:
            connection.execute("DELETE FROM runs WHERE ended < ?", (cutoff,))
            return connection.execute(
                "DELETE FROM samples WHERE ts < ?", (cutoff,)
            ).rowcount

    def record_runs(self, usage: Iterable[Mapping[str, object]]) -> int:
        """Store usage-log runs once each, keyed by run id; return the new ones.

        A run is a machine-source usage entry: ``name`` is its unique log key
        (``usage:<ticket>:<ai>:<end>``), ``end`` its unix time. Runs already
        stored are left untouched, so every collector round may pass the whole
        24-hour log.
        """
        rows = []
        for run in usage:
            try:
                rows.append(
                    (
                        str(run["name"]), int(run["end"]), str(run["ai"]),
                        str(run.get("role") or "unknown"),
                        float(run.get("minutes") or 0), int(run.get("why") == "ok"),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        with self._connect() as connection:
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO runs "
                "(run_id, ended, ai, role, minutes, ok) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            return connection.total_changes - before

    def usage_panel(self, now: int) -> list[dict[str, object]]:
        """Aggregate stored runs per AI and role over 24 hours, 7 days and 30 days.

        Each row has ``ai``, ``role`` and, for every window ``w`` in
        ``24h``/``7d``/``30d``, ``runs<w>``, ``minutes<w>`` and ``bad<w>``
        (runs whose usage entry did not end with ``ok``). Rows are sorted by
        AI name and role; pairs with no run in 30 days are absent.
        """
        widest = max(days for _, days in USAGE_WINDOWS)
        with self._connect() as connection:
            found = connection.execute(
                "SELECT ai, role, ended, minutes, ok FROM runs "
                "WHERE ended > ? AND ended <= ?",
                (now - widest * 86400, now),
            ).fetchall()
        rows: dict[tuple[str, str], dict[str, object]] = {}
        for ai, role, ended, minutes, ok in found:
            row = rows.setdefault((ai, role), {"ai": ai, "role": role})
            for label, days in USAGE_WINDOWS:
                row.setdefault("runs" + label, 0)
                row.setdefault("minutes" + label, 0.0)
                row.setdefault("bad" + label, 0)
                if ended > now - days * 86400:
                    row["runs" + label] += 1
                    row["minutes" + label] = round(row["minutes" + label] + minutes, 1)
                    row["bad" + label] += 0 if ok else 1
        return [rows[key] for key in sorted(rows)]

    def query(
        self, metric: str, since: int = 0, limit: int | None = None
    ) -> list[tuple[int, float]]:
        """Return ``(ts, value)`` pairs of a metric from ``since``, oldest first.

        With ``limit`` only the newest ``limit`` pairs are returned.
        """
        sql = "SELECT ts, value FROM samples WHERE metric = ? AND ts >= ?"
        if limit is None:
            sql += " ORDER BY ts"
            args: tuple = (metric, since)
        else:
            sql = "SELECT * FROM ({} ORDER BY ts DESC LIMIT ?) ORDER BY ts".format(sql)
            args = (metric, since, limit)
        with self._connect() as connection:
            return [(row[0], row[1]) for row in connection.execute(sql, args)]

    def machine_history(self, limit: int = 144) -> list[dict[str, object]]:
        """Return the newest load/memory points in the page's ``history`` shape."""
        mem = dict(self.query("machine.mem_used", limit=limit))
        return [
            {
                "t": dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "load": value,
                "mem": mem.get(ts),
            }
            for ts, value in self.query("machine.load", limit=limit)
        ]
