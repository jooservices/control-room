# control-room

Read-only live view of the AI Flow (Jira project `JOOCR`). One page, always up to date, that shows every project, its progress and ETA, the tickets in the flow, the AI runs, the AI models, the worker machine and the event feed, with links out to Jira, GitHub, pull requests and CI. It never writes to Jira, GitHub or the worker machine.

New here (person or AI)? Read [docs/DESIGN.md](docs/DESIGN.md) first, then the ai-flow guide `ai-flow/docs/CONTEXT.md`.

## Parts
1. **Collector**: a service on the WSL workstation. Every 30 to 60 seconds it reads Jira, GitHub and the worker machine and writes one snapshot (JSON) plus a history.
2. **Web server**: standard library only, GET only. Serves the page, `/api/snapshot`, `/api/history` and a live stream (server-sent events). LAN only.
3. **Frontend**: one static page, no build step, light and dark theme, works on a phone.

## Rules
- Read-only everywhere. Jira and GitHub tokens are read-only and live in the ignored `.env`. Nothing secret is ever shown on the page or written to a snapshot.
- Python 3 standard library only. Tests run with `python3 -m unittest discover -s tests`.
- Conventional Commits, pull request to `develop`, CI green.
