"""Read-only Jira collection for project summaries and recent status events."""

import base64
import datetime as dt
from collections import Counter, defaultdict
from typing import Mapping, Sequence
from urllib.parse import quote, urlencode, urljoin

from controlroom import http
from controlroom.config import Config


# Jira project metadata follows the existing collector prototype. Each tuple is
# (display name, repository, Jira board id).
PROJECTS = {
    "JAIF": ("AI Flow", "ai-flow", 191),
    "JAVM": ("JAVmeta", "javmeta", 186),
    "JDTO": ("DTO", "dto", None),
    "JOOC": ("Client", "client", None),
    "JOOCR": ("Control Room", "control-room", None),
    "JOOCX": ("CrawlerX", "crawlerx", 187),
    "JOODT": ("Draytek", "draytek", None),
    "JOOF": ("Flickr", "flickr", None),
    "JOONETM": ("Network Management", "network-management", None),
    "JOOS": ("JOOservices", "Workspaces", None),
    "JOOSX": ("StoryX", "storyx", None),
    "JOOUA": ("Useragent", "useragent", None),
    "JOOWT": ("JOOservices WP Themes", "jooservices-wp-themes", None),
}

_ISSUE_FIELDS = "summary,status,issuetype,priority,parent,updated"
_SEARCH_PAGE_SIZE = 100
_CHANGELOG_PAGE_SIZE = 100
_MAX_ACTIVE_TICKETS = 30
_MAX_RECENT_DONE_TICKETS = 5
_CLOSED_STATUS_NAMES = {"Done", "Declined"}
_UTC = dt.timezone.utc


def collect_jira(
    config: Config,
    flow_project_keys: Sequence[str],
    *,
    now: dt.datetime,
    project_definitions: Mapping[str, tuple[str, str, int | None]] = PROJECTS,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    """Return the projects document and recent status events.

    Issue searches cover every configured project. Changelogs are requested
    only for issues updated in the last day and only when their project key is
    in ``flow_project_keys``.
    """
    flow_keys = _validated_flow_keys(flow_project_keys, project_definitions)
    _require_aware(now)

    issues_by_project = {}
    histories_by_issue = {}
    cutoff = now.astimezone(_UTC) - dt.timedelta(hours=24)
    for project_key in project_definitions:
        issues = fetch_project_issues(config, project_key)
        issues_by_project[project_key] = issues
        if project_key not in flow_keys:
            continue

        for issue in issues:
            updated = _parse_jira_datetime(issue["fields"]["updated"])
            if cutoff <= updated <= now.astimezone(_UTC):
                histories_by_issue[issue["key"]] = _fetch_issue_changelogs(
                    config, issue["key"]
                )

    return (
        parse_projects_document(
            issues_by_project,
            flow_keys,
            now=now,
            project_definitions=project_definitions,
        ),
        parse_status_events(
            issues_by_project, histories_by_issue, flow_keys, now=now
        ),
    )


def fetch_project_issues(config: Config, project_key: str) -> list[dict[str, object]]:
    """Fetch all issues for one project using Jira's token-paged search API."""
    _validate_project_key(project_key)
    jql = "project = {} ORDER BY key ASC".format(project_key)
    issues = []
    next_page_token = None
    seen_tokens = set()

    while True:
        parameters = {
            "jql": jql,
            "maxResults": _SEARCH_PAGE_SIZE,
            "fields": _ISSUE_FIELDS,
        }
        if next_page_token is not None:
            if next_page_token in seen_tokens:
                raise ValueError("Jira search repeated a nextPageToken")
            seen_tokens.add(next_page_token)
            parameters["nextPageToken"] = next_page_token

        response = _get_json(config, "rest/api/3/search/jql", parameters)
        page_issues = response.get("issues", [])
        if not isinstance(page_issues, list):
            raise ValueError("Jira search response has invalid issues")
        issues.extend(page_issues)

        next_page_token = response.get("nextPageToken")
        if response.get("isLast") is True or not next_page_token:
            return issues


def parse_projects_document(
    issues_by_project: Mapping[str, Sequence[dict[str, object]]],
    flow_project_keys: Sequence[str],
    *,
    now: dt.datetime,
    project_definitions: Mapping[str, tuple[str, str, int | None]] = PROJECTS,
) -> dict[str, object]:
    """Build the prototype-compatible projects document without HTTP calls."""
    flow_keys = set(flow_project_keys)
    current_time = _require_aware(now).astimezone(_UTC)
    cutoff = current_time - dt.timedelta(hours=24)
    projects = []

    for project_key, (name, repository, board) in project_definitions.items():
        issues = list(issues_by_project.get(project_key, ()))
        counts = Counter()
        children_by_epic = defaultdict(list)
        epics = []
        open_tickets = []
        done_tickets = []
        updates = []

        for issue in issues:
            fields = issue["fields"]
            status = fields.get("status") or {}
            status_name = status.get("name", "")
            issue_type = (fields.get("issuetype") or {}).get("name", "")
            updated_raw = fields.get("updated", "")
            updated = _parse_jira_datetime(updated_raw) if updated_raw else None
            counts[status_name] += 1
            if updated is not None:
                updates.append((updated, updated_raw))

            if issue_type.casefold() == "epic":
                epics.append(
                    {
                        "key": issue["key"],
                        "summary": (fields.get("summary") or "")[:90],
                        "status": status_name,
                        "children": 0,
                        "done": 0,
                    }
                )
                continue

            parent = fields.get("parent") or {}
            parent_key = parent.get("key") if isinstance(parent, dict) else None
            if parent_key:
                children_by_epic[parent_key].append(status_name)

            if status_name == "Done" and updated is not None:
                done_tickets.append((updated, issue))
            if not _is_closed(status):
                sort_time = updated or dt.datetime.min.replace(tzinfo=_UTC)
                open_tickets.append((sort_time, issue))

        for epic in epics:
            child_statuses = children_by_epic.get(epic["key"], [])
            epic["children"] = len(child_statuses)
            epic["done"] = sum(status == "Done" for status in child_statuses)

        active = [
            _active_ticket(issue)
            for _, issue in sorted(
                open_tickets,
                key=lambda pair: (pair[0], pair[1]["key"]),
                reverse=True,
            )[:_MAX_ACTIVE_TICKETS]
        ]
        recent_done = [
            {
                "key": issue["key"],
                "summary": (issue["fields"].get("summary") or "")[:90],
                "closed": (issue["fields"].get("updated") or "")[:10],
            }
            for _, issue in sorted(
                done_tickets,
                key=lambda pair: (pair[0], pair[1]["key"]),
                reverse=True,
            )[:_MAX_RECENT_DONE_TICKETS]
        ]
        done_last_24h = sum(
            1
            for updated, issue in done_tickets
            if cutoff <= updated <= current_time
        )
        last_update = max(
            updates,
            default=(None, ""),
            key=lambda pair: pair[0] or dt.datetime.min.replace(tzinfo=_UTC),
        )[1]

        projects.append(
            {
                "key": project_key,
                "name": name,
                "repo": repository,
                "board": board,
                "total": len(issues),
                "counts": dict(sorted(counts.items())),
                "epics": sorted(epics, key=lambda epic: epic["key"]),
                "active": active,
                "recent": recent_done,
                "doneLast24h": done_last_24h,
                "mergedPRs": [],
                "openPRs": [],
                "lastUpdate": last_update[:16],
                "onFlow": project_key in flow_keys,
            }
        )

    return {"list": projects}


def parse_status_events(
    issues_by_project: Mapping[str, Sequence[dict[str, object]]],
    histories_by_issue: Mapping[str, Sequence[dict[str, object]]],
    flow_project_keys: Sequence[str],
    *,
    now: dt.datetime,
) -> list[dict[str, str]]:
    """Return status changes within the rolling 24-hour UTC window."""
    flow_keys = set(flow_project_keys)
    current_time = _require_aware(now).astimezone(_UTC)
    cutoff = current_time - dt.timedelta(hours=24)
    events = []

    for project_key, issues in issues_by_project.items():
        if project_key not in flow_keys:
            continue
        for issue in issues:
            for history in histories_by_issue.get(issue["key"], ()):
                changed_at = _parse_jira_datetime(history["created"])
                if not cutoff <= changed_at <= current_time:
                    continue
                for item in history.get("items", ()):
                    if item.get("field") == "status":
                        events.append(
                            {
                                "at": _format_utc(changed_at),
                                "kind": "status",
                                "key": issue["key"],
                                "text": "{} → {}".format(
                                    item.get("fromString") or "",
                                    item.get("toString") or "",
                                ),
                            }
                        )

    return sorted(
        events,
        key=lambda event: (event["at"], event["key"], event["text"]),
        reverse=True,
    )


def _fetch_issue_changelogs(config: Config, issue_key: str) -> list[dict[str, object]]:
    histories = []
    start_at = 0
    seen_offsets = set()

    while True:
        if start_at in seen_offsets:
            raise ValueError("Jira changelog repeated a startAt offset")
        seen_offsets.add(start_at)
        response = _get_json(
            config,
            "rest/api/3/issue/{}/changelog".format(quote(issue_key, safe="")),
            {"startAt": start_at, "maxResults": _CHANGELOG_PAGE_SIZE},
        )
        page_histories = response.get("values", [])
        if not isinstance(page_histories, list):
            raise ValueError("Jira changelog response has invalid values")
        histories.extend(page_histories)

        next_start_at = start_at + len(page_histories)
        total = response.get("total")
        if response.get("isLast") is True or (
            isinstance(total, int) and next_start_at >= total
        ):
            return histories
        if not page_histories:
            raise ValueError("Jira changelog returned an empty non-final page")
        start_at = next_start_at


def _get_json(
    config: Config, endpoint: str, parameters: Mapping[str, object]
) -> dict[str, object]:
    base_url = config.jira_site.rstrip("/") + "/"
    url = urljoin(base_url, endpoint) + "?" + urlencode(parameters)
    credentials = "{}:{}".format(config.jira_user, config.jira_token).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
    }
    response = http.get_json(url, headers=headers)
    if not isinstance(response, dict):
        raise ValueError("Jira API returned a non-object response")
    return response


def _validated_flow_keys(
    flow_project_keys: Sequence[str],
    project_definitions: Mapping[str, tuple[str, str, int | None]],
) -> set[str]:
    flow_keys = set()
    for project_key in flow_project_keys:
        _validate_project_key(project_key)
        if project_key not in project_definitions:
            raise ValueError(
                "flow project key is not configured: {}".format(project_key)
            )
        flow_keys.add(project_key)
    return flow_keys


def _validate_project_key(project_key: str) -> None:
    if not project_key or not project_key[0].isalpha() or not project_key.isupper():
        raise ValueError("Jira project key must be uppercase letters and digits")
    if not project_key.replace("_", "").isalnum():
        raise ValueError("Jira project key must be uppercase letters and digits")


def _is_closed(status: Mapping[str, object]) -> bool:
    if status.get("name") in _CLOSED_STATUS_NAMES:
        return True
    category = status.get("statusCategory")
    return isinstance(category, dict) and category.get("key") == "done"


def _active_ticket(issue: dict[str, object]) -> dict[str, object]:
    fields = issue["fields"]
    parent = fields.get("parent") or {}
    return {
        "key": issue["key"],
        "type": (fields.get("issuetype") or {}).get("name", ""),
        "status": (fields.get("status") or {}).get("name", ""),
        "priority": (fields.get("priority") or {}).get("name", ""),
        "summary": (fields.get("summary") or "")[:100],
        "parent": parent.get("key") if isinstance(parent, dict) else None,
        "updated": (fields.get("updated") or "")[:16],
    }


def _parse_jira_datetime(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def _format_utc(value: dt.datetime) -> str:
    return value.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_aware(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value
