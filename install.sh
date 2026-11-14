#!/usr/bin/env bash
# Install or refresh the agentq laptop services on macOS. Safe to run again.
#
#   1. checks python3 (3.11+), git and the pi CLI
#   2. creates the state folder ($AGENTQ_HOME, default ~/.local/state/agentq) and broker.json
#   3. stores the Morph API key in the macOS Keychain, if it is not there yet (hidden prompt)
#   4. renders the launchd templates in agentq/launchd and loads them: broker, proxy,
#      dashboard and housekeeping
#
# On Linux steps 1 and 2 run and the launchd step is skipped; see the README.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTQ_HOME="${AGENTQ_HOME:-$HOME/.local/state/agentq}"
AGENTQ_HOME="${AGENTQ_HOME/#\~/$HOME}"
KEYCHAIN_SERVICE="morph-api-key"
DASHBOARD_PORT=18791
AGENTS_DIR="$HOME/Library/LaunchAgents"
SERVICES=(broker proxy dash housekeeping)

say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }

say "== prerequisites"
PY="$(command -v "${AGENTQ_PYTHON:-python3}" || true)"
[ -n "$PY" ] || die "python3 not found; install Python 3.11 or newer"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "$("$PY" -V) is too old; Python 3.11 or newer is required"
PY="$("$PY" -c 'import sys; print(sys.executable)')"
say "python: $PY ($("$PY" -V))"
command -v git >/dev/null || die "git not found on PATH"
say "git: $(git --version)"
PI_BIN="$(command -v pi || true)"
[ -n "$PI_BIN" ] || die "the pi CLI is not on PATH; install the pi coding agent first (see README, Setup)"
say "pi: $PI_BIN"

say "== state folder: $AGENTQ_HOME"
mkdir -p "$AGENTQ_HOME/logs" "$AGENTQ_HOME/slots" "$AGENTQ_HOME/jobs"
if [ -e "$AGENTQ_HOME/broker.json" ]; then
  say "broker.json: kept (existing file)"
else
  cp "$REPO/broker.example.json" "$AGENTQ_HOME/broker.json"
  say "broker.json: written from broker.example.json"
fi

if [ "$(uname -s)" != "Darwin" ]; then
  say "== not macOS: skipping Keychain and launchd"
  say "Start the services by hand (see README, Run without launchd)."
  exit 0
fi

say "== Morph API key (macOS Keychain, service $KEYCHAIN_SERVICE)"
if security find-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1; then
  say "key: present"
elif [ -t 0 ]; then
  say "No key stored yet. The next prompt is from macOS; the key is typed hidden and never printed."
  security add-generic-password -a "$USER" -s "$KEYCHAIN_SERVICE" -w \
    || die "could not store the key; run the add command from the README and install again"
  say "key: stored"
else
  die "no key in the Keychain and no terminal to ask on; run: security add-generic-password -a \"\$USER\" -s $KEYCHAIN_SERVICE -w"
fi

say "== launchd services"
mkdir -p "$AGENTS_DIR" "$HOME/.local/state/agentq/logs"
render() {
  sed -e "s|__HOME__|$HOME|g" -e "s|__PYTHON__|$PY|g" \
      -e "s|__REPO__|$REPO|g" -e "s|__AGENTQ_HOME__|$AGENTQ_HOME|g" "$1" > "$2"
}
for name in "${SERVICES[@]}"; do
  label="com.agentq.$name"
  dest="$AGENTS_DIR/$label.plist"
  render "$REPO/agentq/launchd/$label.plist" "$dest"
  plutil -lint "$dest" >/dev/null || die "rendered $dest is not a valid plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$dest"
  say "loaded $label"
done

say ""
say "Dashboard: http://127.0.0.1:$DASHBOARD_PORT/"
say "CLI:       $REPO/bin/agentq status   (or add $REPO/bin to PATH)"
say "Next: submit a first job (README, Get started)."
