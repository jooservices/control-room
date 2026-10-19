# Control Room design

## Goal
A single page, in the LAN only, that is always live and shows the whole AI Flow without anyone opening Jira. It is read-only: it observes, it never changes anything.

## What the page shows
- Key numbers: working now, in the flow, waiting for the owner, done and merged in 24 hours, typical time, rework, machine load.
- Timeline of AI runs (developer, reviewer, QA) and merged pull requests, the last 6 hours; a bar opens the ticket.
- Tickets now by status; every project with progress, a distribution bar, ETA and links (board, repository, pull requests, CI).
- AI models: ready, cooling or out of the pool, runs and minutes in 24 hours, the sign-in probe result.
- Machine: load, memory, disk, workers and heartbeats, state store, containers, flow code version, latest CI runs.
- Event feed (status changes, AI runs, merges) with a filter. Drawers for a ticket, a project and an AI.
- Honest freshness: "updated N seconds ago", turns amber then red when the collector is late.

## Sources (all read-only)
- Jira REST (all projects: counts, epics, active and recent tickets, changelog for flow projects).
- GitHub through the `gh` CLI (pull requests, merges, CI runs).
- The worker machine: Redis state of ai-flow (heartbeats, leases, probe, cooldown, usage log), `/proc`, disk, Docker count, systemd unit states, git head, and the output of `aiflow-work check` to learn which projects the flow watches.

## Parts
1. Collector (systemd user service on WSL, `controlroom.collector`): writes `data/snapshot.json` atomically and appends to a history store (SQLite). A failed source marks only that part stale, never the whole page.
2. Web server (`controlroom.server`): stdlib `http.server`, GET only, binds to the LAN address, serves the static page, `/api/snapshot`, `/api/history?metric=&since=`, `/api/stream` (SSE), `/healthz`.
3. Frontend (`web/`): the existing demo page, reading `/api/*` instead of the artifact database; reconnects by itself.

## Rules
- Read-only tokens, kept in `.env`; never in a snapshot or on the page.
- Standard library only. Tests with `unittest`; the collector's parsing and the ETA maths are pure functions with tests.
- ETA only for projects on the flow, shown as a range with the sample size; never invent a number.
- The page must say when data is old or a source is down.

## Reference
A working prototype of the page and the collector exists in `docs/prototype/` (page.html and collect.py). Treat it as the starting point and the reference for the data shapes, not as final code.
