#!/usr/bin/env bash
# Stop and remove the agentq launchd services. Keeps the state folder and the Keychain key
# unless asked: --purge deletes $AGENTQ_HOME (queued and finished jobs, logs, broker.json).
# Services whose plist points at a different checkout are left alone unless --force.
set -euo pipefail

AGENTQ_HOME="${AGENTQ_HOME:-$HOME/.local/state/agentq}"
AGENTQ_HOME="${AGENTQ_HOME/#\~/$HOME}"
AGENTS_DIR="$HOME/Library/LaunchAgents"
SERVICES=(broker proxy dash housekeeping)
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PURGE=0
FORCE=0
for arg in "$@"; do
  case $arg in
    --purge) PURGE=1 ;;
    --force) FORCE=1 ;;
  esac
done

if [ "$(uname -s)" = "Darwin" ]; then
  for name in "${SERVICES[@]}"; do
    label="com.agentq.$name"
    plist="$AGENTS_DIR/$label.plist"
    if [ "$FORCE" != 1 ]; then
      if [ ! -f "$plist" ]; then
        echo "skipped $label: not installed from here"
        continue
      fi
      if ! grep -qF "$REPO" "$plist"; then
        echo "skipped $label: its plist belongs to another agentq install (pass --force to remove it anyway)"
        continue
      fi
    fi
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$plist"
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
