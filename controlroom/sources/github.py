"""Read pull requests and workflow runs through the GitHub CLI."""

import json
import subprocess
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone


_COMMAND_TIMEOUT_SECONDS = 30
_CI_RUN_LIMIT = 10
_MERGE_WINDOW = timedelta(hours=24)


def parse_gh_output(
    open_pr_output: str,
    merged_pr_output: str,
    ci_run_output: str,
    *,
    now: datetime,
) -> dict[str, list[dict[str, object]]]:
    """Parse recorded `gh` JSON output into the source's plain data shape.

    This function performs no I/O. Invalid JSON or unexpected response shapes
    raise `ValueError`, which the source translates into an error note.
    """
    current_time = _as_utc(now)
    open_pull_requests = _json_list(open_pr_output, "open pull requests")
    merged_pull_requests = _json_list(merged_pr_output, "merged pull requests")
    ci_runs = _json_list(ci_run_output, "workflow runs")

    open_prs: list[dict[str, object]] = []
    for pull_request in open_pull_requests:
        row = _object(pull_request, "open pull request")
        number = _number(row, "number", "open pull request")
        title = _string(row, "title", "open pull request")
        url = _string(row, "url", "open pull request")
        draft = row.get("isDraft")
        if not isinstance(draft, bool):
            raise ValueError("open pull request has an invalid draft flag")
        open_prs.append(
            {"number": number, "title": title, "url": url, "draft": draft}
        )

    recent_merges: list[tuple[datetime, dict[str, object]]] = []
    for pull_request in merged_pull_requests:
        row = _object(pull_request, "merged pull request")
        number = _number(row, "number", "merged pull request")
        title = _string(row, "title", "merged pull request")
        merged_at = _string(row, "mergedAt", "merged pull request")
        url = _string(row, "url", "merged pull request")
        merged_time = _timestamp(merged_at, "merged pull request")
        if current_time - _MERGE_WINDOW <= merged_time <= current_time:
            recent_merges.append(
                (
                    merged_time,
                    {
                        "number": number,
                        "title": title,
                        "mergedAt": merged_at,
                        "url": url,
                    },
                )
            )
    recent_merges.sort(key=lambda merge: merge[0], reverse=True)

    parsed_runs: list[tuple[datetime, dict[str, object]]] = []
    for run in ci_runs:
        row = _object(run, "workflow run")
        name_value = row.get("name") or row.get("displayTitle")
        name = name_value if isinstance(name_value, str) and name_value else "Workflow run"
        url = _string(row, "url", "workflow run")
        created_at = _string(row, "createdAt", "workflow run")
        updated_at = _string(row, "updatedAt", "workflow run")
        created_time = _timestamp(created_at, "workflow run")
        updated_time = _timestamp(updated_at, "workflow run")
        conclusion = row.get("conclusion")
        status = row.get("status")
        if conclusion is not None and not isinstance(conclusion, str):
            raise ValueError("workflow run has an invalid conclusion")
        if status is not None and not isinstance(status, str):
            raise ValueError("workflow run has an invalid status")
        if not conclusion and not status:
            raise ValueError("workflow run has no status")
        parsed_runs.append(
            (
                created_time,
                {
                    "name": name,
                    "url": url,
                    "status": conclusion or status,
                    "mins": round(
                        max(0.0, (updated_time - created_time).total_seconds()) / 60,
                        1,
                    ),
                },
            )
        )
    parsed_runs.sort(key=lambda run: run[0], reverse=True)

    return {
        "openPRs": open_prs,
        "mergedPRs": [merge[1] for merge in recent_merges],
        "ciRuns": [run[1] for run in parsed_runs],
    }


def collect(
    project_repos: Mapping[str, str], *, now: datetime | None = None
) -> dict[str, object]:
    """Collect GitHub data for each configured Jira project.

    Only read-only `gh pr list` and `gh run list` commands are executed. Any
    command or response failure returns empty project data and an error note.
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    empty_result = _empty_result(project_repos)
    projects: dict[str, dict[str, object]] = {}

    for project_key, repository in project_repos.items():
        try:
            open_prs = _run_gh(
                [
                    "pr",
                    "list",
                    "--repo",
                    repository,
                    "--state",
                    "open",
                    "--limit",
                    "100",
                    "--json",
                    "number,title,url,isDraft",
                ]
            )
            merged_prs = _run_gh(
                [
                    "pr",
                    "list",
                    "--repo",
                    repository,
                    "--state",
                    "merged",
                    "--search",
                    "merged:>=" + (now - _MERGE_WINDOW).strftime("%Y-%m-%d"),
                    "--limit",
                    "100",
                    "--json",
                    "number,title,mergedAt,url",
                ]
            )
            ci_runs = _run_gh(
                [
                    "run",
                    "list",
                    "--repo",
                    repository,
                    "--limit",
                    str(_CI_RUN_LIMIT),
                    "--json",
                    "name,displayTitle,url,status,conclusion,createdAt,updatedAt",
                ]
            )
            parsed = parse_gh_output(
                open_prs,
                merged_prs,
                ci_runs,
                now=now,
            )
        except FileNotFoundError:
            return _error_result(empty_result, "GitHub CLI 'gh' is not available.")
        except subprocess.CalledProcessError as error:
            return _error_result(
                empty_result,
                "GitHub command failed for {} (exit code {}).".format(
                    repository, error.returncode
                ),
            )
        except subprocess.TimeoutExpired:
            return _error_result(
                empty_result, "GitHub command timed out for {}.".format(repository)
            )
        except OSError:
            return _error_result(
                empty_result,
                "GitHub command could not start for {}.".format(repository),
            )
        except ValueError:
            return _error_result(
                empty_result,
                "GitHub returned invalid JSON data for {}.".format(repository),
            )

        projects[project_key] = {"repo": repository, **parsed}

    return {"projects": projects, "error": None}


def _run_gh(arguments: list[str]) -> str:
    completed = subprocess.run(
        ["gh", *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )
    return completed.stdout


def _empty_result(project_repos: Mapping[str, str]) -> dict[str, object]:
    return {
        "projects": {
            project_key: {
                "repo": repository,
                "openPRs": [],
                "mergedPRs": [],
                "ciRuns": [],
            }
            for project_key, repository in project_repos.items()
        },
        "error": None,
    }


def _error_result(result: dict[str, object], note: str) -> dict[str, object]:
    return {"projects": result["projects"], "error": note}


def _json_list(output: str, description: str) -> list[object]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError:
        raise ValueError("{} output is not valid JSON".format(description)) from None
    if not isinstance(value, list):
        raise ValueError("{} output must be a JSON array".format(description))
    return value


def _object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("{} entry must be an object".format(description))
    return value


def _number(row: dict[str, object], field: str, description: str) -> int:
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("{} has an invalid {}".format(description, field))
    return value


def _string(row: dict[str, object], field: str, description: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError("{} has an invalid {}".format(description, field))
    return value


def _timestamp(value: str, description: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("{} has an invalid timestamp".format(description)) from None
    if parsed.tzinfo is None:
        raise ValueError("{} timestamp must include a timezone".format(description))
    return parsed.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("now must include a timezone")
    return value.astimezone(timezone.utc)
