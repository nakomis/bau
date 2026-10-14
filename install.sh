#!/usr/bin/env bash
# Install or update BAU on Cere. Run as nakomis from the repo checkout.
set -euo pipefail
ROOT="$HOME/bau"
SRC="$ROOT/src"
mkdir -p "$ROOT"/{repos,work,logs}
if [ "$(pwd)" != "$SRC" ]; then
  echo "Expected to run from $SRC (clone the repo there)"; exit 1
fi
uv sync --frozen
docker build -t bau-agent:latest container/
docker compose -f container/compose.yml up -d --build
for label in bau-run bau-snapshot; do
  plist="$HOME/Library/LaunchAgents/com.nakomis.$label.plist"
  cp "launchd/com.nakomis.$label.plist" "$plist"
  plutil -lint "$plist"
  launchctl bootout "gui/$(id -u)/com.nakomis.$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
done
for item in plane-api-key github-app-key claude-oauth-token; do
  security find-generic-password -s bau -a "$item" >/dev/null 2>&1 || echo "WARNING: keychain item bau/$item missing"
done
echo "Installed. Dry run: $SRC/.venv/bin/bau run --dry-run"
