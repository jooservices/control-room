# Control Room design

## Goal

Control Room is a LAN-only, read-only view of the AI Flow on the WSL workstation. It collects real Jira, GitHub and worker-machine state, keeps a local snapshot and history, and serves a static page that refreshes itself. It does not write to those source systems.

## What the page shows

- Jira projects, ticket counts and flow progress, with links to Jira and repositories.
- AI Flow ticket status, recent activity, worker runs, GitHub pull requests and CI runs.
- AI model availability and worker-machine load, memory, disk, services, heartbeats, Redis state and container count.
- Snapshot freshness, source failures, collector version, loop count and last loop duration.

All displayed source values come from the sources described below. The page does not synthesize simulated tickets, runs or machine metrics. Unknown or unavailable values remain empty or are shown as unknown.

## Components

1. **Collector (`controlroom.collector`)** runs as a systemd user service. It reads the configured interval (45 seconds by default), collects each source, assembles a versioned snapshot and appends successful samples to SQLite history. It atomically replaces `DATA_DIR/snapshot.json` and prunes history samples older than 90 days.
2. **Web server (`controlroom.server`)** runs as a separate systemd user service using Python's standard-library HTTP server. With an empty `BIND_HOST`, it selects the workstation's LAN address; the default port is 8088. It serves the static assets under `web/`, `GET` and `HEAD /api/snapshot`, and `GET` and `HEAD /healthz`. `/healthz?detail=1` adds collector and per-source metadata. Other HTTP methods return 405. The server does not write source data.
3. **Frontend (`web/`)** is a static page with no build step. It fetches `/api/snapshot` and attempts `/api/stream`; when a stream is unavailable it falls back to polling the snapshot every 30 seconds. The server currently exposes no stream or history API route. The page's machine history chart is included in each snapshot.

## Sources

- **Jira REST API** provides project issue counts, epics, active and recent issues, and flow-project status changes. The configured account and token are read-only.
- **GitHub CLI (`gh`)** provides pull requests, merges and workflow runs for configured repositories. Authentication is managed by the local CLI, outside `.env`, and must have read-only access.
- **Worker machine** provides `/proc` load, memory and uptime, filesystem usage, systemd unit states, running container count, AI Flow configuration and Redis state such as heartbeats, leases, probes, cooldowns and usage records. It reads local state only.

## Snapshot and history

The root snapshot contains `version`, `projects`, `flow`, `ai`, `timeline`, `events`, `machine` and `meta`. `controlroom/model.py` defines the empty schema and `controlroom/assembler.py` maps source results into it. Empty defaults mean unknown or not collected; they are not evidence that a source succeeded.

`DATA_DIR` defaults to `./data`, relative to the service working directory (the repository checkout). The collector stores:

- `snapshot.json`: the latest normalized view, written through a temporary file and atomic rename.
- `history.db`: SQLite samples of successful Jira and machine metrics. Samples are appended and pruned to 90 days; stale values are not sampled again as fresh data.

Keep `DATA_DIR` and `.env` when upgrading the checkout. The application has no database migration step.

## Freshness and failures

The collector records `meta.asOf`, `meta.ok`, and `meta.errorAt`. A source failure keeps that source's last known section while other successful sources update; `meta.ok` identifies the sources that succeeded in the latest loop. `errorAt` records the last failure time and clears when that source recovers. `meta.asOf` advances if at least one source succeeds, and stays at its previous value if all sources fail.

`/healthz` compares the snapshot file modification time with three configured collection intervals. It returns 200 for a recent file and 503 for a missing or old one. This measures collector writes, not source freshness: the collector may keep writing a snapshot when every upstream source fails. `/healthz?detail=1` reports source results, `asOf`, and collector metadata so operators can distinguish these cases.

The page shows source failures in a warning. It marks `meta.asOf` amber after 180 seconds and red after 900 seconds. This threshold is separate from `/healthz`; for example, the page can report old data while the collector continues writing snapshots. See the [runbook](RUNBOOK.md) for the operational meaning of each status and recovery steps.

## Deployment and constraints

`deploy/install.sh` renders the service unit templates for the checkout path, reloads the user's systemd manager, enables and starts both services, and restarts units whose definitions changed. The units use `/usr/bin/python3`, restart failed processes after 15 seconds, and start from the repository working directory. User lingering is required to start services after WSL reboots without an interactive login.

The project uses Python 3 standard library only. Tests use `unittest`. Keep Jira and GitHub credentials out of the snapshot and logs; the page receives normalized data only. See the [README](../README.md) for configuration and test commands, and the [runbook](RUNBOOK.md) for installation, operation, upgrades and rollback.
