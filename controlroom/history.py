"""SQLite history of the numbers the page charts and the ETA need."""

import contextlib
import datetime as dt
import re
import sqlite3
from pathlib import Path
from typing import Mapping

RETENTION_DAYS = 90
FIXED_METRICS = frozenset(
    {"machine.load", "machine.mem_used", "flow.done24h", "flow.rework24h"}
)
AI_MINUTES_METRIC = re.compile(r"ai\.[A-Za-z0-9_-]+\.minutes24h")


def is_known_metric(metric: str) -> bool:
    """Tell whether ``metric`` is one of the series the history API serves."""
    return metric in FIXED_METRICS or AI_MINUTES_METRIC.fullmatch(metric) is not None


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
            return connection.execute(
                "DELETE FROM samples WHERE ts < ?", (cutoff,)
            ).rowcount

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
