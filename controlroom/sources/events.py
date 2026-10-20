"""Read-only event metrics from the OpenObserve ``aiflow_events`` stream."""

import base64
import datetime as dt
import math
import statistics
from collections import defaultdict
from typing import Mapping
from urllib.parse import quote, urljoin

from controlroom import http
from controlroom.config import Config


STREAM = "aiflow_events"
PAGE_SIZE = 1000
MAX_ROWS = 100_000
PERIODS = {
    "24h": dt.timedelta(hours=24),
    "7d": dt.timedelta(days=7),
    "30d": dt.timedelta(days=30),
}
_EVENT_TYPES = {"ticket.status", "step.run", "ai.usage"}
_UTC = dt.timezone.utc


def collect(config: Config, *, now: dt.datetime) -> dict[str, object]:
    """Query the last 30 days and return status and AI usage aggregates."""
    _require_aware(now)
    if (
        not config.openobserve_url
        or not config.openobserve_user
        or not config.openobserve_password
    ):
        raise RuntimeError("OpenObserve is not configured")

    start = now.astimezone(_UTC) - PERIODS["30d"]
    start_us = int(start.timestamp() * 1_000_000)
    end_us = int(now.astimezone(_UTC).timestamp() * 1_000_000)
    endpoint = urljoin(
        config.openobserve_url.rstrip("/") + "/",
        "api/{}/_search".format(quote(config.openobserve_org, safe="")),
    )
    credential = "{}:{}".format(
        config.openobserve_user, config.openobserve_password
    ).encode("utf-8")
    headers = {
        "Authorization": "Basic " + base64.b64encode(credential).decode("ascii")
    }
    rows = _search_rows(endpoint, headers, start_us=start_us, end_us=end_us)
    return parse_query_result({"hits": rows}, now=now)


def _search_rows(
    endpoint: str,
    headers: Mapping[str, str],
    *,
    start_us: int,
    end_us: int,
) -> list[dict[str, object]]:
    """Fetch bounded pages from the OpenObserve read-only search endpoint."""
    rows: list[dict[str, object]] = []
    expected_total = None
    sql = 'SELECT * FROM "{}" ORDER BY _timestamp ASC'.format(STREAM)
    for offset in range(0, MAX_ROWS, PAGE_SIZE):
        response = http.search_json(
            endpoint,
            {
                "query": {
                    "sql": sql,
                    "start_time": start_us,
                    "end_time": end_us,
                    "from": offset,
                    "size": PAGE_SIZE,
                }
            },
            headers=headers,
        )
        if not isinstance(response, Mapping):
            raise ValueError("OpenObserve search response must be an object")
        page = response.get("hits")
        if not isinstance(page, list) or any(
            not isinstance(row, Mapping) for row in page
        ):
            raise ValueError("OpenObserve search response has invalid hits")
        total = response.get("total")
        if total is not None:
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise ValueError("OpenObserve search response has invalid total")
            if total < offset + len(page):
                raise ValueError("OpenObserve search response total is inconsistent")
            if expected_total is not None and total != expected_total:
                raise ValueError("OpenObserve search total changed during paging")
            expected_total = total

        rows.extend(dict(row) for row in page)
        if expected_total is not None and len(rows) >= expected_total:
            return rows
        if len(page) < PAGE_SIZE:
            if expected_total is not None and len(rows) < expected_total:
                raise ValueError("OpenObserve search returned an incomplete page")
            return rows

    if expected_total is not None and len(rows) >= expected_total:
        return rows
    raise ValueError("OpenObserve search exceeded the row limit")


def parse_query_result(result: object, *, now: dt.datetime) -> dict[str, object]:
    """Parse OpenObserve hits into duration medians and usage totals.

    Status intervals are formed from consecutive status transitions for the
    same ticket. Runs are counted from ``step.run`` events and tokens from
    ``ai.usage`` events, grouped by AI and role.
    """
    current = _require_aware(now).astimezone(_UTC)
    if not isinstance(result, Mapping) or not isinstance(result.get("hits"), list):
        raise ValueError("OpenObserve search result has invalid hits")

    status_events = []
    runs = []
    token_events = []
    for raw_row in result["hits"]:
        if not isinstance(raw_row, Mapping):
            raise ValueError("OpenObserve search result contains an invalid hit")
        row = _payload(raw_row)
        kind = _event_kind(row)
        if kind not in _EVENT_TYPES:
            continue
        at = _event_time(row)
        if at < current - PERIODS["30d"] or at > current:
            continue
        if kind == "ticket.status":
            key = _ticket_key(row)
            status = _status_value(row)
            if not key or not status:
                raise ValueError("ticket.status event is missing its ticket or status")
            status_events.append({"key": key, "status": status, "at": at})
        elif kind == "step.run":
            ai, role = _ai_and_role(row)
            run_id = _optional_text(row.get("run_id") or row.get("id"))
            runs.append(
                {
                    "id": run_id,
                    "ai": ai,
                    "role": role,
                    "at": at,
                    "minutes": _run_minutes(row),
                }
            )
        else:
            ai, role = _ai_and_role(row)
            token_events.append(
                {
                    "ai": ai,
                    "role": role,
                    "at": at,
                    "tokens": _token_count(row),
                }
            )

    intervals = _status_intervals(status_events)
    return {
        "statusMedianMinutes": _status_medians(intervals, current),
        "usageByRole": _usage_totals(runs, token_events, current),
    }


def _payload(row: Mapping[str, object]) -> dict[str, object]:
    payload = dict(row)
    for name in ("data", "attributes"):
        nested = row.get(name)
        if isinstance(nested, Mapping):
            payload.update(nested)
    nested_event = row.get("event")
    if isinstance(nested_event, Mapping):
        payload.update(nested_event)
    return payload


def _event_kind(row: Mapping[str, object]) -> str:
    for name in (
        "event_type",
        "event.name",
        "event_name",
        "kind",
        "type",
        "name",
        "event",
    ):
        value = row.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _event_time(row: Mapping[str, object]) -> dt.datetime:
    for name in ("at", "timestamp", "time", "_timestamp"):
        if name in row:
            parsed = _parse_time(row[name])
            if parsed is not None:
                return parsed
    raise ValueError("OpenObserve event is missing a valid timestamp")


def _parse_time(value: object) -> dt.datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        if abs(number) >= 1e14:
            number /= 1_000_000
        elif abs(number) >= 1e11:
            number /= 1_000
        try:
            return dt.datetime.fromtimestamp(number, _UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        return _parse_time(number)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def _ticket_key(row: Mapping[str, object]) -> str:
    for name in ("ticket_key", "issue_key", "key", "ticket"):
        value = row.get(name)
        if isinstance(value, Mapping):
            value = value.get("key")
        text = _optional_text(value)
        if text:
            return text
    return ""


def _status_value(row: Mapping[str, object]) -> str:
    transition = row.get("transition")
    if isinstance(transition, Mapping):
        for name in ("to_status", "to", "new_status"):
            value = _status_text(transition.get(name))
            if value:
                return value
    for name in ("to_status", "new_status", "status_to", "to", "status"):
        value = _status_text(row.get(name))
        if value:
            return value
    return ""


def _status_text(value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("name") or value.get("to")
    return _optional_text(value)


def _ai_and_role(row: Mapping[str, object]) -> tuple[str, str]:
    ai = ""
    for name in ("ai", "ai_name", "agent", "model", "actor"):
        value = row.get(name)
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("id")
        ai = _optional_text(value)
        if ai:
            break
    role = _optional_text(row.get("role"))
    if not ai or not role:
        raise ValueError("AI event is missing its AI or role")
    return ai, role


def _optional_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _status_intervals(events: list[dict[str, object]]) -> list[dict[str, object]]:
    by_ticket: dict[str, list[dict[str, object]]] = defaultdict(list)
    for event in events:
        by_ticket[event["key"]].append(event)

    intervals = []
    for ticket_events in by_ticket.values():
        ordered = sorted(ticket_events, key=lambda event: event["at"])
        for previous, following in zip(ordered, ordered[1:]):
            seconds = (following["at"] - previous["at"]).total_seconds()
            if seconds > 0:
                intervals.append(
                    {
                        "status": previous["status"],
                        "ended_at": following["at"],
                        "minutes": seconds / 60,
                    }
                )
    return intervals


def _status_medians(
    intervals: list[dict[str, object]], now: dt.datetime
) -> dict[str, dict[str, float]]:
    results = {}
    for period, duration in PERIODS.items():
        cutoff = now - duration
        by_status: dict[str, list[float]] = defaultdict(list)
        for interval in intervals:
            if cutoff <= interval["ended_at"] <= now:
                by_status[interval["status"]].append(interval["minutes"])
        results[period] = {
            status: round(statistics.median(values), 1)
            for status, values in sorted(by_status.items())
        }
    return results


def _run_minutes(row: Mapping[str, object]) -> float:
    if "minutes" in row:
        return _number(row["minutes"], "run minutes")
    for name, scale in (
        ("duration_seconds", 1 / 60),
        ("duration_s", 1 / 60),
        ("elapsed_seconds", 1 / 60),
        ("duration_ms", 1 / 60_000),
        ("elapsed_ms", 1 / 60_000),
    ):
        if name in row:
            return _number(row[name], "run duration") * scale
    start = _first_time(row, ("started_at", "start_time", "start"))
    end = _first_time(row, ("finished_at", "end_time", "end"))
    if start is not None and end is not None:
        return max(0.0, (end - start).total_seconds() / 60)
    return 0.0


def _first_time(
    row: Mapping[str, object], names: tuple[str, ...]
) -> dt.datetime | None:
    for name in names:
        if name in row:
            parsed = _parse_time(row[name])
            if parsed is not None:
                return parsed
    return None


def _token_count(row: Mapping[str, object]) -> int:
    for name in ("total_tokens", "tokens_total"):
        if name in row:
            return _integer(row[name], "token count")
    tokens = row.get("tokens")
    if isinstance(tokens, Mapping):
        for name in ("total", "total_tokens"):
            if name in tokens:
                return _integer(tokens[name], "token count")
        input_tokens = _mapping_value(
            tokens, ("input", "input_tokens", "prompt", "prompt_tokens")
        )
        output_tokens = _mapping_value(
            tokens,
            ("output", "output_tokens", "completion", "completion_tokens"),
        )
        if input_tokens is not None or output_tokens is not None:
            return _integer(input_tokens or 0, "input tokens") + _integer(
                output_tokens or 0, "output tokens"
            )
    if tokens is not None:
        return _integer(tokens, "token count")
    input_tokens = _mapping_value(
        row, ("input_tokens", "prompt_tokens", "tokens_in")
    )
    output_tokens = _mapping_value(
        row, ("output_tokens", "completion_tokens", "tokens_out")
    )
    if input_tokens is not None or output_tokens is not None:
        return _integer(input_tokens or 0, "input tokens") + _integer(
            output_tokens or 0, "output tokens"
        )
    raise ValueError("ai.usage event is missing its token count")


def _mapping_value(row: Mapping[str, object], names: tuple[str, ...]) -> object:
    for name in names:
        if name in row:
            return row[name]
    return None


def _number(value: object, description: str) -> float:
    if isinstance(value, bool):
        raise ValueError("{} must be a non-negative number".format(description))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            "{} must be a non-negative number".format(description)
        ) from None
    if not math.isfinite(number) or number < 0:
        raise ValueError("{} must be a non-negative number".format(description))
    return number


def _integer(value: object, description: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ValueError("{} must be a non-negative number".format(description))
        return value
    if isinstance(value, str):
        try:
            integer = int(value.strip(), 10)
        except ValueError:
            integer = None
        if integer is not None:
            if integer < 0:
                raise ValueError(
                    "{} must be a non-negative number".format(description)
                )
            return integer
    number = _number(value, description)
    if not number.is_integer():
        raise ValueError("{} must be a whole number".format(description))
    return int(number)


def _usage_totals(
    runs: list[dict[str, object]],
    token_events: list[dict[str, object]],
    now: dt.datetime,
) -> dict[str, list[dict[str, object]]]:
    results = {}
    for period, duration in PERIODS.items():
        cutoff = now - duration
        totals: dict[tuple[str, str], dict[str, object]] = {}
        seen_runs = set()
        for run in runs:
            if not cutoff <= run["at"] <= now:
                continue
            identity = (run["ai"], run["role"], run["id"])
            if run["id"] and identity in seen_runs:
                continue
            if run["id"]:
                seen_runs.add(identity)
            row = totals.setdefault(
                (run["ai"], run["role"]),
                {
                    "ai": run["ai"],
                    "role": run["role"],
                    "runs": 0,
                    "minutes": 0.0,
                    "tokens": 0,
                },
            )
            row["runs"] += 1
            row["minutes"] += run["minutes"]
        for event in token_events:
            if not cutoff <= event["at"] <= now:
                continue
            row = totals.setdefault(
                (event["ai"], event["role"]),
                {
                    "ai": event["ai"],
                    "role": event["role"],
                    "runs": 0,
                    "minutes": 0.0,
                    "tokens": 0,
                },
            )
            row["tokens"] += event["tokens"]
        results[period] = [
            {
                **row,
                "minutes": round(row["minutes"], 1),
            }
            for _, row in sorted(totals.items())
        ]
    return results


def _require_aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value
