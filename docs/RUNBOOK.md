# Control Room operator runbook

This runbook covers the Control Room systemd user services on the WSL workstation. The collector reads Jira, GitHub and the local AI Flow worker machine; the server reads the collector's local snapshot and serves the page. Both services are read-only with respect to those sources.

## Requirements and first install

- Run from the repository checkout on WSL, on a stable path inside the Linux filesystem.
- Python 3, `systemd`, `systemctl`, and `loginctl` must be available. No Python packages need to be installed; the application uses the standard library.
- The user service must have lingering enabled so it starts after a WSL reboot and keeps running without an interactive login. Check this with `loginctl show-user "$USER" --property=Linger`. If it reports `no`, ask the workstation administrator to enable user lingering before installing.
- Configure the read-only Jira account and GitHub CLI access described in the [README](../README.md#configuration). The worker machine source also needs access to the AI Flow configuration and its Redis state.

From the repository root, create `.env` only if it does not already exist, edit it locally, and restrict its permissions:

```sh
cp -n .env.example .env
chmod 600 .env
```

Do not paste `.env` values into logs, ticket comments or support messages. Then install and enable both services:

```sh
./deploy/install.sh
```

The installer renders the unit files into `~/.config/systemd/user`, reloads systemd, enables and starts `controlroom-collector.service` and `controlroom-server.service`, and restarts a service if its unit definition changed. It exits with an error if `.env` is missing, required commands are unavailable, or user lingering is disabled.

## Start, stop and status

Run these commands from any directory in the same WSL user session:

```sh
systemctl --user start controlroom-collector.service controlroom-server.service
systemctl --user stop controlroom-collector.service controlroom-server.service
systemctl --user status controlroom-collector.service controlroom-server.service
```

Start runs both services; stop temporarily disables collection and the web page until they are started again. The install step enables them for the user's systemd session. To inspect recent service messages or follow new ones:

```sh
journalctl --user -u controlroom-collector.service -u controlroom-server.service --since "15 minutes ago"
journalctl --user -u controlroom-collector.service -u controlroom-server.service -f
```

The collector runs immediately and then at `COLLECT_INTERVAL_SECONDS` (45 seconds by default). Both units restart a failed process after 15 seconds.

## Check the page and `/healthz`

Set `SERVER_ADDRESS` to the address printed in the server journal (or the configured `BIND_HOST`) and `SERVER_PORT` to the configured `PORT` (8088 by default). From a machine that can reach the WSL LAN address:

```sh
SERVER_ADDRESS='replace-with-server-address'
SERVER_PORT=8088
python3 -c 'import http.client,sys; c=http.client.HTTPConnection(sys.argv[1],int(sys.argv[2]),timeout=5); c.request("GET",sys.argv[3]); r=c.getresponse(); print(r.status); print(r.read().decode()); c.close()' \
  "$SERVER_ADDRESS" "$SERVER_PORT" /healthz
python3 -c 'import http.client,sys; c=http.client.HTTPConnection(sys.argv[1],int(sys.argv[2]),timeout=5); c.request("GET",sys.argv[3]); r=c.getresponse(); print(r.status); print(r.read().decode()); c.close()' \
  "$SERVER_ADDRESS" "$SERVER_PORT" '/healthz?detail=1'
```

The short response contains `ok` and `ageSeconds`. HTTP 200 means the snapshot file was written within three collection intervals; with the default 45-second interval, the limit is 135 seconds. HTTP 503 means no snapshot exists or its file modification time is at least that old. `ageSeconds` is the snapshot file age, not the age of the external Jira or GitHub data.

The detail response adds:

- `asOf`: timestamp of the latest collection in which at least one source succeeded.
- `collector`: running code version, loop count, and duration of the last completed loop, plus cumulative failure counts per source. These counts do not reset on recovery; use `sources` and `errorAt` for current source status.
- `sources`: whether Jira, GitHub and the worker machine succeeded in the last collection.
- `errorAt`: timestamp of each source's most recent failure. A source's entry is removed after that source succeeds again.

`/healthz` measures snapshot writes, not source success. If all sources fail but the collector keeps writing snapshots, `/healthz` can still return 200 while `asOf` and the page's freshness badge become old. Check `sources` and the page warning as well.

## Alert and status meanings

| Page or endpoint status | Meaning | Operator action |
| --- | --- | --- |
| `Live` / normal footer | `meta.asOf` is no more than 3 minutes old. | No action. |
| Amber `Last update` / amber footer | `meta.asOf` is older than 3 minutes. | Check for a source warning and inspect collector service status and logs. |
| Red `Last update` / red footer | `meta.asOf` is older than 15 minutes. | Treat displayed data as old; check both services and their source access. |
| `Waiting for the first snapshot` or `Collector lag: unknown` | No usable collection timestamp has been loaded yet, or the snapshot request failed. | Check that both services are active and the collector has completed a collection. |
| `Last collection failed for Jira, GitHub, or Worker machine` | That source failed in the latest collection. Its prior figures are retained and may be stale; successful sources continue to update. | Use `/healthz?detail=1` and the collector journal to identify the source, then follow the stale-source steps below. |
| `/healthz` returns HTTP 503 | Snapshot is missing or its file has not been rewritten within three configured collection intervals. | Check collector status, logs, configuration and data-directory permissions. |
| Machine panel says the worker machine did not answer | The machine source did not provide load metrics in the last snapshot. | Check the worker machine source using the stale-source steps. |
| Amber or red machine gauge | The bar is above 55% (amber) or 80% (red) of its reference capacity: CPU count for load, total memory for memory, or total disk for disk. | Check whether the high utilization is sustained. The color alone does not mean a service failed. |
| AI state is `cooling` or `out of pool` | `cooling` means the agent is in a cooldown; `out of pool` means its probe marked it unusable and the worker automatically replaces it. | Check the named probe and cooldown state in the AI Flow operator's normal workflow. This is not a Control Room server failure. |
| Red outline on an AI run | The recorded run ended badly. | Follow up on that run in its AI Flow ticket; this does not by itself mean collection failed. |

The page's freshness colors use `meta.asOf`: amber above 180 seconds and red above 900 seconds. The `/healthz` threshold is different and uses the file modification time and configured collection interval.

## Troubleshoot a stale source

1. Request `http://<server-address>:8088/healthz?detail=1` and note which entry in `sources` is false and its `errorAt` timestamp. The dashboard warning uses the same source names.
2. Check the collector service and recent journal without copying its output into a public ticket:

   ```sh
   systemctl --user status controlroom-collector.service
   journalctl --user -u controlroom-collector.service --since "15 minutes ago"
   ```

3. Check only the affected dependency: Jira site, account and network access for Jira; `gh` authentication and repository read access for GitHub; AI Flow checkout/configuration, Redis access and local Linux metrics for the worker machine. Keep credential values private.
4. Correct the local configuration or dependency, then wait at least one collection interval. Request `/healthz?detail=1` again. A recovered source becomes `true`, its `errorAt` entry clears, and its figures refresh. Do not delete the snapshot or history to clear a warning.

If the snapshot is being written but all source entries are false, the collector is alive and its dependencies need attention. The UI may continue showing old values until at least one source succeeds.

## After a WSL reboot

User lingering and the installer's `enable` step allow both services to start when the user has no interactive session. After WSL is available, check:

```sh
systemctl --user status controlroom-collector.service controlroom-server.service
SERVER_ADDRESS='replace-with-server-address'
SERVER_PORT=8088
python3 -c 'import http.client,sys; c=http.client.HTTPConnection(sys.argv[1],int(sys.argv[2]),timeout=5); c.request("GET",sys.argv[3]); r=c.getresponse(); print(r.status); print(r.read().decode()); c.close()' \
  "$SERVER_ADDRESS" "$SERVER_PORT" /healthz
```

If either unit is inactive, start both with the start command above. If lingering is disabled, the services may not start until login; ask the workstation administrator to enable it. If the health check fails, use the status and journal commands above. The LAN address may change after reboot; use the current address printed in the server journal.

## Upgrade

Upgrade the deployed WSL checkout to the approved `develop` revision. Keep the checkout path, `.env`, and `DATA_DIR` unchanged. Save the current commit ID so it can be restored if needed, then update code and restart both services:

```sh
CHECKOUT=/path/to/control-room
PREVIOUS_COMMIT=$(git -C "$CHECKOUT" rev-parse HEAD)
git -C "$CHECKOUT" fetch origin develop
git -C "$CHECKOUT" switch --detach origin/develop
"$CHECKOUT/deploy/install.sh"
systemctl --user restart controlroom-collector.service controlroom-server.service
```

Record `PREVIOUS_COMMIT` outside the shell if it may close before verification. The installer's unit comparison restarts services only when unit files change; the explicit restart applies changed Python and web files. Check `/healthz`, then `/healthz?detail=1` and the page. No database migration is needed; `history.db` is retained and old samples are pruned by normal collection.

## Roll back

Use the saved commit ID in the same checkout. This example expects `CHECKOUT` and `PREVIOUS_COMMIT` from the upgrade step; if using a new shell, set them to the deployed checkout path and recorded commit ID first.

```sh
git -C "$CHECKOUT" switch --detach "$PREVIOUS_COMMIT"
"$CHECKOUT/deploy/install.sh"
systemctl --user restart controlroom-collector.service controlroom-server.service
```

Check `/healthz`, the detailed source status and the page after rollback. Leave `.env`, `DATA_DIR`, `snapshot.json` and `history.db` in place. There is no schema rollback step.
