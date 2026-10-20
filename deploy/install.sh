#!/usr/bin/env bash
# Install and enable the Control Room systemd user services.
set -Eeuo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
UNIT_DIR="$HOME/.config/systemd/user"
USER_NAME="${USER:?USER must be set to install user services}"

if [[ ! -f "$REPO_ROOT/.env" ]]; then
  printf 'Missing configuration file: %s/.env\n' "$REPO_ROOT" >&2
  exit 1
fi

for command in loginctl systemctl python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$command" >&2
    exit 1
  fi
done

linger="$(loginctl show-user "$USER_NAME" --property=Linger --value)"
if [[ "$linger" != yes ]]; then
  printf 'User lingering is disabled for %s. Enable it with: loginctl enable-linger %s\n' \
    "$USER_NAME" "$USER_NAME" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"
changed=()
for unit in controlroom-collector.service controlroom-server.service; do
  target="$UNIT_DIR/$unit"
  temporary="$(mktemp "$UNIT_DIR/.${unit}.XXXXXX")"
  if ! python3 "$REPO_ROOT/deploy/render_units.py" \
    "$REPO_ROOT/deploy/$unit" "$REPO_ROOT" > "$temporary"; then
    rm -f -- "$temporary"
    exit 1
  fi
  chmod 0644 "$temporary"
  if [[ -f "$target" ]] && cmp -s "$temporary" "$target"; then
    rm -f -- "$temporary"
  else
    mv -f -- "$temporary" "$target"
    changed+=("$unit")
  fi
done

systemctl --user daemon-reload
systemctl --user enable --now \
  controlroom-collector.service controlroom-server.service
# enable --now leaves a running service on its old definition; restart the ones whose unit changed.
if ((${#changed[@]})); then
  systemctl --user restart "${changed[@]}"
fi

printf 'Enabled Control Room user services. Check status with:\n'
printf '  systemctl --user status controlroom-collector.service controlroom-server.service\n'
printf '  journalctl --user -u controlroom-collector.service -u controlroom-server.service -f\n'
printf 'Linger status for %s: ' "$USER_NAME"
loginctl show-user "$USER_NAME" --property=Linger
