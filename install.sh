#!/usr/bin/env bash
# Install or update BAU on Cere. Run as nakomis from the repo checkout.
set -euo pipefail
for tool in pnpm uv docker; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: $tool not found on PATH; install it first. Nothing has been changed."; exit 1; }
done
ROOT="$HOME/bau"
SRC="$ROOT/src"
mkdir -p "$ROOT"/{repos,work,logs}
if [ "$(pwd)" != "$SRC" ]; then
  echo "Expected to run from $SRC (clone the repo there)"; exit 1
fi
uv sync --frozen
docker build --load -t bau-agent:latest container/
docker compose -f container/compose.yml up -d --build
if [ -f web/package.json ]; then
  (cd web && pnpm install --frozen-lockfile && pnpm build)
else
  echo "WARNING: web/package.json missing; skipping the dashboard build"
fi
for label in bau-run bau-snapshot bau-sweep bau-api; do
  if [ "$label" = bau-api ] && ! security find-generic-password -s bau -a proxy-token >/dev/null 2>&1; then
    echo "!!! SKIPPING com.nakomis.bau-api: keychain item bau/proxy-token is missing."
    echo "!!! Add it (security add-generic-password -s bau -a proxy-token -w) and re-run install.sh."
    continue
  fi
  plist="$HOME/Library/LaunchAgents/com.nakomis.$label.plist"
  cp "launchd/com.nakomis.$label.plist" "$plist"
  plutil -lint "$plist"
  launchctl bootout "gui/$(id -u)/com.nakomis.$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
done
for item in plane-api-key github-app-key claude-oauth-token proxy-token; do
  security find-generic-password -s bau -a "$item" >/dev/null 2>&1 || echo "WARNING: keychain item bau/$item missing"
done
echo "Installed. Dry run: $SRC/.venv/bin/bau run --dry-run"
