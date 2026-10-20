"""Collector entry point: ``python3 -m controlroom.collector [--once]``."""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping

from controlroom.assembler import assemble
from controlroom.config import Config, ConfigurationError, load_config
from controlroom.history import HistoryStore, snapshot_samples
from controlroom.sources import github, jira, machine

SNAPSHOT_FILE = "snapshot.json"
HISTORY_FILE = "history.db"


def write_snapshot(path: str | Path, snapshot: Mapping[str, object]) -> None:
    """Write ``snapshot`` as JSON through a temp file and an atomic rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=target.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def read_snapshot(path: str | Path) -> dict[str, object] | None:
    """Return the previous snapshot, or None when missing or unreadable."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def collect_sources(
    config: Config, previous: Mapping[str, object] | None, now: dt.datetime
) -> dict[str, object]:
    """Run every source; a source that raises is stored as its exception."""
    results: dict[str, object] = {}

    def run(name: str, call: Callable[[], object]) -> None:
        try:
            results[name] = call()
        except Exception as error:  # one failing source must not stop the others
            results[name] = error

    run("machine", lambda: machine.collect(now=int(now.timestamp())))
    if isinstance(results["machine"], Mapping):
        flow_keys = list(results["machine"].get("flowProjects") or [])
    else:
        old_meta = (previous or {}).get("meta") or {}
        flow_keys = list(old_meta.get("flowProjects") or [])
    old_project_document = (previous or {}).get("projects")
    old_projects = (
        old_project_document.get("list")
        if isinstance(old_project_document, Mapping)
        else []
    )
    if not isinstance(old_projects, list):
        old_projects = []
    cycle_cache = {
        row["key"]: row["etaCycleCache"]
        for row in old_projects
        if isinstance(row, Mapping)
        and isinstance(row.get("key"), str)
        and isinstance(row.get("etaCycleCache"), Mapping)
    }
    run(
        "jira",
        lambda: jira.collect_jira(
            config,
            flow_keys,
            now=now,
            cycle_cache_by_project=cycle_cache,
        ),
    )
    run("github", lambda: github.collect(config.github_project_repos, now=now))
    return results


def git_head() -> str | None:
    """Return the short git head of this checkout, or None when unavailable."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent, capture_output=True,
            text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def collector_meta(
    snapshot: Mapping[str, object], previous: Mapping[str, object] | None,
    duration: float, version: str | None,
) -> dict[str, object]:
    """Build ``meta.collector``: loop count, last loop duration, source errors.

    Counts continue from the previous snapshot, so they survive a restart.
    """
    old = ((previous or {}).get("meta") or {}).get("collector") or {}
    old_errors = old.get("sourceErrors") or {}
    errors = {}
    for name, good in snapshot["meta"]["ok"].items():
        errors[name] = int(old_errors.get(name, 0)) + (0 if good else 1)
    return {
        "version": version,
        "loopCount": int(old.get("loopCount", 0)) + 1,
        "lastLoopSeconds": round(duration, 3),
        "sourceErrors": errors,
    }


def run_once(
    config: Config,
    *,
    now: dt.datetime | None = None,
    collect: Callable[..., Mapping[str, object]] = collect_sources,
    version: str | None = None,
) -> dict[str, object]:
    """Collect, write the snapshot atomically, append and prune the history."""
    began = time.monotonic()
    now = now or dt.datetime.now(dt.timezone.utc)
    snapshot_path = config.data_dir / SNAPSHOT_FILE
    previous = read_snapshot(snapshot_path)
    results = collect(config, previous, now)

    store = HistoryStore(config.data_dir / HISTORY_FILE)
    snapshot = assemble(results, previous, now=now)

    # Insert this round's samples first so the chart history includes them.
    samples = snapshot_samples(snapshot)
    if samples:
        store.insert(int(now.timestamp()), samples)
    store.prune(now)
    snapshot["machine"]["history"] = store.machine_history()
    snapshot["meta"]["collector"] = collector_meta(
        snapshot, previous, time.monotonic() - began, version
    )
    write_snapshot(snapshot_path, snapshot)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="controlroom.collector")
    parser.add_argument("--once", action="store_true", help="collect once and exit")
    parser.add_argument("--env-file", default=".env", help="configuration file")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.env_file)
    except ConfigurationError as error:
        print("Configuration error: {}".format(error), file=sys.stderr)
        return 2

    interval = config.interval_seconds
    version = git_head()
    while True:
        started = time.monotonic()
        try:
            snapshot = run_once(config, version=version)
            print("collected asOf={} ok={}".format(
                snapshot["meta"]["asOf"], snapshot["meta"]["ok"]
            ), flush=True)
        except Exception as error:  # keep the loop alive on a failed round
            print("collect round failed: {}".format(type(error).__name__),
                  file=sys.stderr, flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(max(0.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
