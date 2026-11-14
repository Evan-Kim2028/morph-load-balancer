#!/usr/bin/env bash
# Stop and remove the agentq launchd services. Keeps the state folder and the Keychain key
# unless asked: --purge deletes $AGENTQ_HOME (queued and finished jobs, logs, broker.json).
set -euo pipefail

AGENTQ_HOME="${AGENTQ_HOME:-$HOME/.local/state/agentq}"
AGENTQ_HOME="${AGENTQ_HOME/#\~/$HOME}"
AGENTS_DIR="$HOME/Library/LaunchAgents"
SERVICES=(broker proxy dash housekeeping)
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if [ "$(uname -s)" = "Darwin" ]; then
  for name in "${SERVICES[@]}"; do
    label="com.agentq.$name"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$AGENTS_DIR/$label.plist"
    echo "removed $label"
  done
fi

if [ "$PURGE" = 1 ]; then
  rm -rf "$AGENTQ_HOME"
  echo "deleted $AGENTQ_HOME"
else
  echo "kept $AGENTQ_HOME (pass --purge to delete it)"
fi
echo "Keychain key kept. To remove it: security delete-generic-password -a \"\$USER\" -s morph-api-key"
