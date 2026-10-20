# control-room

Read-only live view of the AI Flow (Jira project `JOOCR`). One page, always up to date, that shows every project, its progress and ETA, the tickets in the flow, the AI runs, the AI models, the worker machine and the event feed, with links out to Jira, GitHub, pull requests and CI. It never writes to Jira, GitHub or the worker machine.

New here (person or AI)? Read [docs/DESIGN.md](docs/DESIGN.md) first, then the ai-flow guide `ai-flow/docs/CONTEXT.md`.

## Parts
1. **Collector**: a service on the WSL workstation. Every 30 to 60 seconds it reads Jira, GitHub and the worker machine and writes one snapshot (JSON) plus a history.
2. **Web server**: standard library only, GET only. Serves the page, `/api/snapshot`, `/api/history` and a live stream (server-sent events). LAN only.
3. **Frontend**: one static page, no build step, light and dark theme, works on a phone.

## Rules
- Read-only everywhere. The Jira API token lives in the ignored `.env`; GitHub uses the `gh` CLI's credential store. Both credentials must be read-only, and no secrets are shown on the page or written to a snapshot.
- Python 3 standard library only. Tests run with `python3 -m unittest discover -s tests`.
- Conventional Commits, pull request to `develop`, CI green.

## Credentials and read-only permissions

Use a dedicated Jira service account with the **Browse Projects** permission only
for the projects configured for collection. Its Jira API token should have the
`read:jira-work` scope and no write or administration scopes. Jira's project and
issue security permissions still limit which issues the account can read. The
collector makes only `GET` requests. See Atlassian's [Jira API token scope
guidance](https://support.atlassian.com/user-management/docs/manage-api-tokens-for-service-accounts/)
and [Jira REST API scopes](https://developer.atlassian.com/cloud/jira/platform/scopes-for-oauth-2-3LO-and-forge-apps/).

Authenticate `gh` with a fine-grained GitHub token limited to the repositories
listed in `GITHUB_REPOS` and `GITHUB_PROJECT_REPOS`. Grant only repository
**Metadata: read**, **Pull requests: read**, and **Actions: read**; do not grant
write permissions. These are the permissions used by `gh pr list` and
`gh run list`. See GitHub's [fine-grained token permission
reference](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
and the [`gh pr list`](https://cli.github.com/manual/gh_pr_list) and
[`gh run list`](https://cli.github.com/manual/gh_run_list) command references.

Keep the Jira site, account, and token in the ignored `.env`; never include
configuration values in the snapshot. The snapshot is the only data boundary
for the page and API: expose normalized status and metrics only, use the generic
machine label `Workstation`, and do not include raw logs or diagnostic details.
