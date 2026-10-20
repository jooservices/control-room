"""Collect read-only state and resource metrics from the local worker machine."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol


USAGE_WINDOW_SECONDS = 24 * 60 * 60
_FLOW_ROOT_DEFAULT = Path.home() / "Workspaces/JOOservices/projects/ai-flow"
_PROJECT_KEY = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Z][A-Z0-9]{1,}):(?P<value>.*)$"
)
_REPO_FIELD = re.compile(r"\brepo\s*[:=]", re.IGNORECASE)


class MachineSourceError(RuntimeError):
    """Raised when required local AI Flow state cannot be read."""


class RedisReader(Protocol):
    """Read-only subset of the AI Flow Redis client used by this source."""

    def get(self, key: str) -> str | bytes | None: ...

    def scan(self, pattern: str) -> Iterable[str]: ...


def parse_loadavg(text: str) -> list[float]:
    """Parse the first three load averages from ``/proc/loadavg``."""
    values = text.split()
    if len(values) < 3:
        raise ValueError("/proc/loadavg must contain three load averages")
    return [float(value) for value in values[:3]]


def parse_meminfo(text: str) -> dict[str, float]:
    """Parse total and available memory from Linux ``/proc/meminfo`` in GiB."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, separator, raw_value = line.partition(":")
        if not separator or name not in {"MemTotal", "MemAvailable"}:
            continue
        fields = raw_value.split()
        if not fields:
            continue
        values[name] = int(fields[0])

    if not {"MemTotal", "MemAvailable"}.issubset(values):
        raise ValueError("/proc/meminfo must contain MemTotal and MemAvailable")
    return {
        "total": round(values["MemTotal"] / 1_048_576, 1),
        "avail": round(values["MemAvailable"] / 1_048_576, 1),
    }


def parse_uptime(text: str) -> float:
    """Parse ``/proc/uptime`` and return uptime in hours."""
    values = text.split()
    if not values:
        raise ValueError("/proc/uptime is empty")
    return round(float(values[0]) / 3600, 1)


def parse_proc_metrics(proc_root: str | Path = "/proc") -> dict[str, object]:
    """Read and parse the local Linux load, memory, and uptime files."""
    root = Path(proc_root)
    return {
        "load": parse_loadavg((root / "loadavg").read_text(encoding="utf-8")),
        "mem": parse_meminfo((root / "meminfo").read_text(encoding="utf-8")),
        "uptime_h": parse_uptime((root / "uptime").read_text(encoding="utf-8")),
    }


def parse_usage_log(
    entries: Mapping[str, str | bytes | None]
    | Iterable[tuple[str, str | bytes | None]],
    *,
    now: int,
    window_seconds: int = USAGE_WINDOW_SECONDS,
) -> list[dict[str, object]]:
    """Decode valid ``usage:<...>:<unix-time>`` JSON entries from the time window."""
    items = entries.items() if isinstance(entries, Mapping) else entries
    runs: list[dict[str, object]] = []
    for key, raw_value in items:
        if not key.startswith("usage:") or raw_value is None:
            continue

        try:
            end = int(key.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if end < now - window_seconds or end > now:
            continue

        try:
            value_text = (
                raw_value.decode("utf-8")
                if isinstance(raw_value, bytes)
                else raw_value
            )
            value = json.loads(value_text)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(value, dict):
            continue

        run = dict(value)
        run["name"] = key
        run["end"] = end
        runs.append(run)

    return sorted(runs, key=lambda run: run["end"])


def parse_systemd_output(text: str) -> dict[str, str]:
    """Parse ``systemctl list-units`` rows into flow unit active states."""
    services: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split(maxsplit=4)
        if len(fields) < 4:
            continue
        unit, _load, active_state, _sub_state = fields[:4]
        if unit.startswith("aiflow-"):
            services[unit] = active_state
    return services


def parse_watched_projects(text: str) -> list[str]:
    """Find project keys whose AI Flow config entries contain a repo field."""
    projects: list[str] = []
    candidate: tuple[str, int] | None = None
    for line in text.splitlines():
        match = _PROJECT_KEY.match(line)
        if match:
            candidate = (match.group("key"), len(match.group("indent")))
            if _REPO_FIELD.search(match.group("value")):
                projects.append(candidate[0])
                candidate = None
            continue

        if candidate is None:
            continue
        indent = len(line) - len(line.lstrip())
        if indent > candidate[1] and _REPO_FIELD.search(line):
            projects.append(candidate[0])
            candidate = None

    return list(dict.fromkeys(projects))


def _flow_root(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    configured_path = os.environ.get("AI_FLOW_ROOT")
    if configured_path:
        return Path(configured_path).expanduser().resolve()
    return _FLOW_ROOT_DEFAULT


def _load_redis_client(flow_root: Path) -> RedisReader:
    """Load the AI Flow Redis client without exposing its configuration."""
    flow_path = str(flow_root)
    if flow_path not in sys.path:
        sys.path.insert(0, flow_path)
    try:
        from aiflow.config import load
        from aiflow.lock import Redis

        config = load()
        return Redis(
            config.redis_host,
            config.redis_port,
            config.redis_db,
            config.redis_prefix,
            config.redis_password,
            config.redis_user,
        )
    except Exception:
        raise MachineSourceError(
            "Could not initialize the AI Flow Redis client"
        ) from None


def _read_text(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _read_redis_state(redis: RedisReader, now: int) -> dict[str, object]:
    heartbeats: dict[str, int] = {}
    for key in redis.scan("hb:*"):
        value = _read_text(redis.get(key))
        try:
            if value is not None:
                heartbeats[key] = max(0, now - int(value))
        except ValueError:
            continue

    leases = {
        key[len("lease:") :]: _read_text(redis.get(key))
        for key in redis.scan("lease:*")
    }
    probes = {
        key[len("probe:") :]: _read_text(redis.get(key))
        for key in redis.scan("probe:*")
    }
    cooldown = {
        key[len("cooldown:") :]: True for key in redis.scan("cooldown:*")
    }
    usage_values = {
        key: redis.get(key) for key in redis.scan("usage:*")
    }
    return {
        "hb": heartbeats,
        "leases": leases,
        "probe": probes,
        "cooldown": cooldown,
        "usage": parse_usage_log(usage_values, now=now),
    }


def _run_command(
    args: list[str], *, cwd: Path | None = None, timeout: float = 20
) -> str | None:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def discover_flow_projects(flow_root: str | Path | None = None) -> list[str]:
    """Read watched project keys from the local AI Flow supervisor config check."""
    root = _flow_root(flow_root)
    output = _run_command(
        [sys.executable, "-m", "aiflow.supervisor", "check"], cwd=root
    )
    if output is None:
        raise MachineSourceError("Could not read the AI Flow configuration")
    return parse_watched_projects(output)


def _disk_usage(path: Path) -> dict[str, int]:
    stats = os.statvfs(path)
    return {
        "total": round(stats.f_blocks * stats.f_frsize / 1_000_000_000),
        "used": round(
            (stats.f_blocks - stats.f_bfree) * stats.f_frsize / 1_000_000_000
        ),
    }


def collect(
    *,
    now: int | None = None,
    proc_root: str | Path = "/proc",
    flow_root: str | Path | None = None,
    redis_client: RedisReader | None = None,
) -> dict[str, object]:
    """Collect the worker machine and AI Flow state using local read-only calls."""
    timestamp = int(time.time()) if now is None else now
    root = _flow_root(flow_root)
    redis = redis_client if redis_client is not None else _load_redis_client(root)
    state = _read_redis_state(redis, timestamp)
    proc = parse_proc_metrics(proc_root)

    disk = _disk_usage(Path.home())
    container_output = _run_command(
        ["docker", "ps", "--format", "{{.Names}}"]
    )
    systemd_output = _run_command(
        [
            "systemctl",
            "--user",
            "list-units",
            "--all",
            "--no-legend",
            "--plain",
            "--full",
            "--no-pager",
            "aiflow-*",
        ]
    )
    code_output = _run_command(
        ["git", "rev-parse", "--short", "HEAD"], cwd=root
    )

    return {
        "now": timestamp,
        **state,
        "load": proc["load"],
        "cpus": os.cpu_count(),
        "mem": proc["mem"],
        "disk": disk,
        "uptime_h": proc["uptime_h"],
        "containers": (
            sum("workspaces-shared" in line for line in container_output.splitlines())
            if container_output is not None
            else None
        ),
        "services": parse_systemd_output(systemd_output)
        if systemd_output is not None
        else None,
        "code": code_output.strip() if code_output is not None else None,
        "flowProjects": discover_flow_projects(root),
        "host": "Workstation",
    }
