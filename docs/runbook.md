# BAU runbook (Cere)

Operating notes for the nightly BAU worker on Cere. Everything runs as the `nakomis` user.

## Prerequisites

- Homebrew, with `uv` installed (`brew install uv`).
- Colima running. Export `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock` in the shell that runs `install.sh`.
- The `claude` CLI at `~/.local/bin/claude`, logged in as the `nakomis` user.

## Layout on Cere

| Path | Contents |
|------|----------|
| `~/bau/src` | The git checkout. `install.sh` must be run from here. |
| `~/bau/repos` | Bare clones of the target repositories |
| `~/bau/work` | Per-session checkouts |
| `~/bau/logs` | `bau-run.log`, `bau-snapshot.log`, and per-session logs |
| `~/bau/state.json` | Snapshot read by the dashboard |
| `~/.config/bau/` | Plane client certificate and key (`cere.crt.pem`, `cere.key.pem`) |

## Keychain items

Secrets are read from the macOS login keychain, service `bau` (`bau/secrets.py`). Never put them in a file in the repo.

- `plane-api-key`: the Plane API key for the worker identity.
  `security add-generic-password -s bau -a plane-api-key -w`
- `github-app-key`: the GitHub App's private key (PEM).
  `security add-generic-password -s bau -a github-app-key -w "$(cat key.pem)"`
- `claude-oauth-token`: the token from `claude setup-token`.
  `security add-generic-password -s bau -a claude-oauth-token -w`

Check an item exists with `security find-generic-password -s bau -a <account>`. `install.sh` warns if any is missing.

## Configuration

`config/bau.yaml` holds the policy and the non-secret identifiers:

- `github_app.app_id` and `github_app.installation_id`: set these to the real values from the GitHub App. They ship as `0`.
- `plane.client_cert` and `plane.client_key`: paths to the Plane mTLS client pair, under `~/.config/bau/`.

## First install

```bash
git clone git@github.com:nakomis/bau.git ~/bau/src
cd ~/bau/src
./install.sh
```

`install.sh` creates `~/bau/{repos,work,logs}`, runs `uv sync --frozen`, builds the `bau-agent:latest` image from `container/Dockerfile`, starts the egress proxy with `docker compose -f container/compose.yml up -d`, and installs and bootstraps the launchd jobs.

## Dry run

Run this once the keychain items and GitHub App ids are in place:

```bash
cd ~/bau/src
.venv/bin/bau probe            # prints the current usage windows (RateLimit(...))
.venv/bin/bau run --dry-run    # lists what a real run would do, starts no sessions
```

`bau probe` prints `None` when no rate-limit event arrives from Claude Code (telemetry missing); the nightly run then records `telemetry: missing`.

A dry run prints a `preflight:` line, then one line per planned action (`feedback`, `ready`, `merged`, or `new-ticket BAU-n -> <repo> (<model>)`), or a decline. If nothing is planned it prints `nothing to do`. It does not write `~/bau/state.json`. Other CLI commands: `bau run` (the real nightly run) and `bau snapshot` (refresh `state.json` without starting sessions). All accept `--config <dir>`, which defaults to `/Users/nakomis/bau/src/config`.

## Schedule

- `com.nakomis.bau-run`: 01:00 every night, runs `bau run`.
- `com.nakomis.bau-snapshot`: hourly from 07:30 to 23:30, runs `bau snapshot`.

No new sessions start after the `no_new_sessions_after` cut-off in `config/bau.yaml` (05:00, Europe/London).

## Pause

Set `paused: true` in `config/bau.yaml` to stop new sessions. Policy refuses to start work while it is set. Set it back to `false` to resume.

The run reads `bau.yaml` once, when `bau run` starts. A change therefore takes effect at the next run, not in a run already in progress. For an immediate stop, see "Stop a run now".

## Stop a run now

```bash
launchctl bootout gui/$(id -u)/com.nakomis.bau-run
docker ps --format '{{.Names}}' | grep '^bau-' | grep -v '^bau-egress$' | xargs -r docker kill
```

`bootout` stops the `bau` process but not a running agent container, so the second line kills any containers named `bau-<ref>`. The egress proxy (`bau-egress`) is left running.

The job stays unloaded until `./install.sh` or `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nakomis.bau-run.plist` is run.

## Logs

```bash
less +F ~/bau/logs/bau-run.log          # the nightly run (stdout and stderr)
less +F ~/bau/logs/bau-snapshot.log     # the hourly snapshot
ls ~/bau/logs/                          # per-session logs: <date>-<REF>.log
docker logs bau-egress                  # egress proxy: denied requests
```

## Upgrade

Do not upgrade while a run is active, roughly 01:00 to 05:30: `install.sh` reloads both jobs and would interrupt it. Use "Stop a run now" first if one is in progress.

```bash
cd ~/bau/src
git pull
./install.sh
docker compose -f container/compose.yml up -d --build
```

`install.sh` does not rebuild the egress proxy when its image already exists, so the final `--build` line is needed after a change to `container/`.

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.nakomis.bau-run
launchctl bootout gui/$(id -u)/com.nakomis.bau-snapshot
rm ~/Library/LaunchAgents/com.nakomis.bau-run.plist ~/Library/LaunchAgents/com.nakomis.bau-snapshot.plist
cd ~/bau/src && docker compose -f container/compose.yml down
```

The `rm` line stops launchd loading the jobs again at login.
