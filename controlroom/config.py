"""Read and validate the collector's local environment configuration."""

from dataclasses import dataclass, field
from pathlib import Path
import re
from urllib.parse import urlsplit


DEFAULT_INTERVAL_SECONDS = 45
DEFAULT_PORT = 8088


class ConfigurationError(ValueError):
    """Raised when the collector configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Validated settings loaded from a dotenv-style file.

    Jira credentials are excluded from the representation so the configuration
    object can be inspected safely in diagnostics.
    """

    jira_site: str
    jira_user: str = field(repr=False)
    jira_token: str = field(repr=False)
    github_repos: tuple[str, ...]
    data_dir: Path
    interval_seconds: int
    github_project_repos: dict[str, str] = field(default_factory=dict)
    bind_host: str | None = None  # None: the server picks the machine's LAN address
    port: int = DEFAULT_PORT


_REQUIRED_VARIABLES = (
    "JIRA_SITE",
    "JIRA_READ_ONLY_USER",
    "JIRA_READ_ONLY_TOKEN",
    "GITHUB_REPOS",
    "DATA_DIR",
)


def _parse_env(path: Path) -> dict[str, str]:
    """Parse KEY=value lines without including values in raised errors."""
    if not path.is_file():
        raise ConfigurationError(f"Configuration file does not exist: {path}")

    values: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        key, separator, raw_value = stripped.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ConfigurationError(f"Malformed configuration line {line_number}")

        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split("#", 1)[0].strip()
        values[key] = value
    return values


def load_config(env_file: str | Path = ".env") -> Config:
    """Load required collector settings from ``env_file``.

    Missing or invalid values raise :class:`ConfigurationError`. Error text
    identifies variable names and line numbers only; it never includes values.
    """
    values = _parse_env(Path(env_file))
    missing = [name for name in _REQUIRED_VARIABLES if not values.get(name, "").strip()]
    if missing:
        raise ConfigurationError(
            "Missing required configuration: " + ", ".join(missing)
        )

    jira_site = values["JIRA_SITE"].strip().rstrip("/")
    try:
        parsed_site = urlsplit(jira_site)
        hostname = parsed_site.hostname
        port = parsed_site.port
    except ValueError:
        raise ConfigurationError(
            "JIRA_SITE must be a valid HTTP or HTTPS URL"
        ) from None
    if (
        parsed_site.scheme not in {"http", "https"}
        or not parsed_site.netloc
        or not hostname
        or port == 0
        or parsed_site.username
        or parsed_site.password
        or parsed_site.query
        or parsed_site.fragment
    ):
        raise ConfigurationError("JIRA_SITE must be an HTTP or HTTPS URL")

    github_repos = tuple(repo.strip() for repo in values["GITHUB_REPOS"].split(","))
    if any(
        not repo
        or repo.count("/") != 1
        or not all(repo.split("/"))
        or any(character.isspace() for character in repo)
        for repo in github_repos
    ):
        raise ConfigurationError(
            "GITHUB_REPOS entries must use owner/repository form"
        )

    github_project_repos: dict[str, str] = {}
    raw_project_repos = values.get("GITHUB_PROJECT_REPOS", "")
    if raw_project_repos.strip():
        for entry in raw_project_repos.split(","):
            project_key, separator, repository = entry.partition("=")
            project_key = project_key.strip()
            repository = repository.strip()
            parts = repository.split("/")
            if (
                not separator
                or not re.fullmatch(r"[A-Z][A-Z0-9]*", project_key)
                or project_key in github_project_repos
                or len(parts) != 2
                or not all(parts)
                or any(character.isspace() for character in repository)
            ):
                raise ConfigurationError(
                    "GITHUB_PROJECT_REPOS entries must use unique PROJECT=owner/repository mappings"
                )
            github_project_repos[project_key] = repository

    try:
        interval_seconds = int(
            values.get("COLLECT_INTERVAL_SECONDS", "").strip()
            or DEFAULT_INTERVAL_SECONDS
        )
    except ValueError:
        raise ConfigurationError(
            "COLLECT_INTERVAL_SECONDS must be an integer"
        ) from None
    if interval_seconds <= 0:
        raise ConfigurationError("COLLECT_INTERVAL_SECONDS must be greater than zero")

    bind_host = values.get("BIND_HOST", "").strip() or None

    try:
        port = int(values.get("PORT", "").strip() or DEFAULT_PORT)
    except ValueError:
        raise ConfigurationError("PORT must be an integer") from None
    if not 0 <= port <= 65535:
        raise ConfigurationError("PORT must be between 0 and 65535")

    return Config(
        jira_site=jira_site,
        jira_user=values["JIRA_READ_ONLY_USER"].strip(),
        jira_token=values["JIRA_READ_ONLY_TOKEN"].strip(),
        github_repos=github_repos,
        data_dir=Path(values["DATA_DIR"].strip()).expanduser(),
        interval_seconds=interval_seconds,
        github_project_repos=github_project_repos,
        bind_host=bind_host,
        port=port,
    )
