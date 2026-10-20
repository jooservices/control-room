# Control Room

Control Room is a read-only live view of the AI Flow. It shows Jira projects and tickets, GitHub pull requests and CI, AI runs, and the WSL worker machine. The collector reads these sources and writes a local snapshot; the web server serves that snapshot and the static page. Nothing in Control Room writes to Jira, GitHub, or the worker machine.

## Run it

After configuring `.env` as described below, install and start the WSL user services from the repository root:

```sh
./deploy/install.sh
systemctl --user status controlroom-collector.service controlroom-server.service
```

The collector runs once per `COLLECT_INTERVAL_SECONDS` (45 seconds by default). The server listens on the configured `BIND_HOST` and `PORT` (8088 by default); when `BIND_HOST` is empty, it selects the machine's LAN address. See the [operator runbook](docs/RUNBOOK.md) for start and stop commands, health checks, upgrades, rollback and troubleshooting.

## Configuration

Copy `.env.example` to `.env` and set the read-only Jira account and token. `gh` must already be authenticated with read access to the repositories in `GITHUB_REPOS`; GitHub credentials are not stored in `.env`.

| Setting | Required | Purpose |
| --- | --- | --- |
| `JIRA_SITE` | Yes | Jira site URL. |
| `JIRA_READ_ONLY_USER` | Yes | Jira account email. |
| `JIRA_READ_ONLY_TOKEN` | Yes | Jira API token for that read-only account. |
| `GITHUB_REPOS` | Yes | Comma-separated `owner/repository` list used by the GitHub collector. |
| `GITHUB_PROJECT_REPOS` | No | Comma-separated `PROJECT=owner/repository` mapping for project links and pull requests. |
| `DATA_DIR` | Yes | Directory for the snapshot and history; defaults to `./data` relative to the service working directory. |
| `COLLECT_INTERVAL_SECONDS` | No | Collection interval; defaults to 45 seconds. |
| `BIND_HOST` | No | Explicit server bind address; empty selects the LAN address. |
| `PORT` | No | Web port; defaults to 8088. |

Keep `.env` private. It is ignored by Git. Do not put credentials in a snapshot or paste them into logs or support messages.

## Data directory

`DATA_DIR` contains:

- `snapshot.json`: latest normalized data used by the page and API.
- `history.db`: SQLite metric samples used for machine history and trends. Samples older than 90 days are pruned during collection.

The collector creates the directory and writes the snapshot atomically. Keep this directory and `.env` across code upgrades; neither is part of the tracked checkout. There is no schema migration step.

## Checks

Run the test suite with `python3 -m unittest discover -s tests -v`. CI also runs `python3 -m compileall -q controlroom` and a server smoke test against a fixture snapshot.

## Documentation

- [Runbook](docs/RUNBOOK.md): install, operate, upgrade, roll back and troubleshoot.
- [Design](docs/DESIGN.md): current components, sources, data flow and failure behavior.
