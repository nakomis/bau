# BAU runbook (Cere)

Operating notes for the nightly BAU worker on Cere. Everything runs as the `nakomis` user.

## Prerequisites

- Homebrew, with `uv` installed (`brew install uv`).
- Colima running. The launchd plists set `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock`; export the same in an interactive shell that runs `docker` by hand.
- The `claude` CLI at `~/.local/bin/claude`, logged in as the `nakomis` user.

No `pnpm`, Node or `docker build` is needed on Cere: CI builds the dashboard, the wheel and the images.

## Layout on Cere

`BAU_HOME` defaults to `~/Library/Application Support/BAU` (the path has a space; the plists and compose file quote it). Nothing under it is a git repository, and nothing is edited by hand except `state/paused`.

| Path | Contents |
|------|----------|
| `$BAU_HOME/releases/<version>/` | One unpacked release bundle: `venv/`, `launchd/`, `compose.yml`, `.env`, `VERSION` |
| `$BAU_HOME/current` | Symlink to the live release; the launchd jobs run `current/venv/bin/bau` |
| `$BAU_HOME/config/host.yaml` | Host facts, deep-merged over the policy in the wheel (`bau/defaults.yaml`). Rendered by Ansible |
| `$BAU_HOME/config/cere.crt.pem`, `cere.key.pem` | Plane client certificate and key (mode 0600) |
| `$BAU_HOME/state/` | `state.json` (snapshot read by the dashboard), `paused`, `atc-cache.json`, `run.lock` |
| `$BAU_HOME/repos` | Bare clones of the target repositories |
| `$BAU_HOME/work` | Per-session checkouts |
| `~/Library/Logs/BAU` | `bau-run.log`, `bau-snapshot.log`, `bau-api.log`, and per-session logs |

## Keychain items

Secrets are read from the macOS login keychain, service `bau` (`bau/secrets.py`). Never put them in a file in the repo.

- `plane-api-key`: the Plane API key for the worker identity.
  `security add-generic-password -s bau -a plane-api-key -w`
- `github-app-key`: the GitHub App's private key (PEM).
  `security add-generic-password -s bau -a github-app-key -w "$(cat key.pem)"`
- `claude-oauth-token`: the token from `claude setup-token`.
  `security add-generic-password -s bau -a claude-oauth-token -w`
- `proxy-token`: the shared secret Leia sends as `X-Bau-Proxy-Token` for `POST /abandon/{ref}`.
  `security add-generic-password -s bau -a proxy-token -w "$(openssl rand -hex 32)"`
  The same value goes in SSM `/bau/proxy-token` for Leia.

Check an item exists with `security find-generic-password -s bau -a <account>`. The Cere playbook asserts they are present.

## Configuration

Policy (schedule, limits, models, identities, the GitHub App ids, the Plane and ATC URLs) lives in `bau/defaults.yaml`, packaged in the wheel. It changes by pull request and release, so a rollback restores the policy that went with the code. It is never edited on the host.

Only host facts go in `$BAU_HOME/config/host.yaml`, which is deep-merged over the defaults. It is optional; `config/host.example.yaml` documents every key you may override (the Plane client certificate paths, `paths.*`, `container.image`). An unknown top-level key is an error. Paths may use `${BAU_HOME}` and `~`.

The projects BAU works on come only from ATC; there is no project file (see "Adding a repository to BAU"). Pausing is only the `state/paused` file (see "Pause").

## Install

Cere installs through the home-servers Ansible play (tracked as BAU-25), which fetches the release bundle from the GitHub release `bau-v<version>`, verifies `SHA256SUMS`, builds the venv from the hashed requirements, pulls the two images, smoke-tests the new release, then flips `current` and reloads the jobs. The version is the `bau_version` pin in home-servers `cere/ansible`. Every Jedi playbook run goes through the midichlorian MCP.

Until that play lands, Cere stays on its old checkout install, pinned by home-infra #484; `install.sh` no longer exists in this repository. Retiring `~/bau/src.retired` is BAU-26.

The release bundle, `bau-<version>.tar.gz`, holds the wheel, a hashed `requirements.txt`, the launchd plist templates (`@BAU_HOME@` and `@HOME@` placeholders), `compose.yml` for the egress proxy, `INSTALL`, `VERSION` (version, SHA and the two image refs) and `SHA256SUMS`. The images are `registry.home.nakomis.com/nakomis/bau-agent:<version>-<sha>` and `bau-egress:<version>-<sha>`, `linux/arm64`.

Check what is running with `bau version` (prints `bau <version> (<sha>)`; `unstamped` for a development checkout); the dashboard footer shows the same version.

## Development loop

From a checkout, with no install:

```bash
uv sync
cp config/host.example.yaml config/host.yaml   # then uncomment what you need; gitignored
uv run bau --config config/ run --dry-run
```

`config/host.yaml` is not committed. A checkout reports `unstamped` from `bau version` and uses the dashboard in `web/dist` if `pnpm build` has been run. The agent image is derived from the build stamp, so set `container.image` in `host.yaml` (for example `bau-agent:dev`) to use a locally built one.

## Dry run

Run this once the keychain items and GitHub App ids are in place:

```bash
BAU=~/Library/"Application Support"/BAU/current/venv/bin/bau
$BAU probe            # prints the current usage windows (RateLimit(...))
$BAU run --dry-run    # lists what a real run would do, starts no sessions
```

`bau probe` prints `None` when no rate-limit event arrives from Claude Code (telemetry missing); the nightly run then records `telemetry: missing`.

A dry run prints a `preflight:` line, then one line per planned action (`feedback`, `ready`, `merged`, or `new-ticket BAU-n -> <repo> (<model>)`), or a decline. If nothing is planned it prints `nothing to do`. It does not write `state/state.json`. Other CLI commands: `bau run` (the real nightly run) and `bau snapshot` (refresh `state.json` without starting sessions). All accept `--config <dir>`, the directory holding `host.yaml`, which defaults to `$BAU_HOME/config`. `bau version` prints the build version.

## Schedule

- `com.nakomis.bau-run`: 01:00 every night, runs `bau run`. The sweep, triage and questions are steps of the night, not separate jobs.
- `com.nakomis.bau-snapshot`: hourly from 07:30 to 23:30, runs `bau snapshot`.
- `com.nakomis.bau-api`: kept alive, runs `bau serve`.

No new sessions start after the `no_new_sessions_after` cut-off in `bau/defaults.yaml` (05:00, Europe/London). The plists are templates in the release bundle, so a schedule change is a pull request and a release, and rolls back with the code.

## Sweep

Each night the sweep reads the open items of every BAU-enabled project in ATC and asks an agent which are candidates for BAU: small and well specified, stale, duplicates, or already done by a merged BAU PR. The result is a summary item named "Sweep <Month YYYY>" in the BAU project, plus a comment on each candidate saying why it was suggested. A second sweep in the same month adds to that item instead of creating another. See `docs/superpowers/specs/2026-10-18-bau-questions-design.md` for the details.

The sweep only proposes. It never adds the `BAU` label and never changes an item's state; Martin labels the candidates he agrees with.

Label an item `not-bau` to stop the sweep suggesting it. The sweep also skips its own "Sweep <Month YYYY>" summaries, and it quotes the agent's reasons as `the sweep agent said: "..."` with brackets and link schemes stripped.

Its session wall clock is `limits.sweep_session_wall_clock_minutes` in `bau/defaults.yaml`. It shares `state/run.lock` with the night and `bau abandon`: `bau run` waits up to an hour for the lock and, if it still cannot start, records `stop_reason: locked` in `state.json` and exits 0.

## Pause

```bash
echo "away until Monday" > "$HOME/Library/Application Support/BAU/state/paused"   # or just: touch it
rm "$HOME/Library/Application Support/BAU/state/paused"                          # resume
```

While `state/paused` (`paths.pause_file`) exists, policy refuses to start new sessions. Its contents, if any, are the reason; `bau` logs it to stderr (the run log) when it first sees the pause. This file is the only pause switch: there is no `paused` key in the YAML, and the dashboard writes the same file.

A running `bau run` re-reads the pause file before every session, so a pause takes effect before the next session starts; a session already in progress runs to its end. Merged PRs are still closed out and green drafts still marked ready while paused, since neither starts a session. For an immediate stop, see "Stop a run now".

## Stop a run now

```bash
launchctl bootout gui/$(id -u)/com.nakomis.bau-run
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock   # an interactive shell may not have this
ids=$(docker ps -q --filter label=bau.session=1)
[ -n "$ids" ] && echo "$ids" | xargs docker kill
```

`bootout` sends `bau` SIGTERM. `bau` turns that into a clean exit: it kills the running agent container, writes `state.json` with `stop_reason: failed: SystemExit: 143`, and exits. The last two lines are a backstop for when `bau` could not clean up (for example after a SIGKILL): they kill any container labelled `bau.session=1`, which every agent session carries. The egress proxy (`bau-egress`) has no such label and is left running.

Only one `bau run` runs at a time: a second one finds `state/run.lock` held, prints `another bau run is in progress` and exits 0.

The job stays unloaded until the Ansible play (tag `bau`) or `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.nakomis.bau-run.plist` is run.

## Dashboard and API

`bau serve` runs under launchd as `com.nakomis.bau-api` on port 8800, behind Leia (which adds mTLS and the proxy token). It serves `/state.json`, the built dashboard (packaged in the wheel as `bau/web_dist`), and `POST /abandon/{ref}`.

The dashboard is a single page at `https://bau.home.nakomis.com/` (mTLS, Nakomis Home CA client certificate). It shows an overall tier, open PRs grouped by repository (each linking to GitHub and Plane), the last run, what is waiting to deploy, and the snapshot's age. The tier mirrors BD-1's BAU row (`mac/conversation-memory-forwarder/src/status.rs` in home-servers; the same rules live in `web/src/lib/tier.ts` and must change together):

| Tier | When |
|------|------|
| red | `state.json` cannot be loaded; snapshot older than 26 h, more than 5 minutes in the future, or with no usable `generated_at`; last run stopped with `failed*`, `preflight*` or `rejected`; 30 or more open PRs |
| amber | no snapshot yet; usage telemetry missing; 10 or more open PRs |
| green | otherwise |

"Failed runs" means the last run only: the snapshot carries `last_run`, not a history.

**Not wired yet (waits on Cere's LAN address, HOME-445).** Until Leia's `cere_lan_ip` is set, Leia's vhost answers every request with `503 "BAU is not provisioned yet"` (served as `text/plain`) and never reaches this API; BD-1 hides its BAU row on exactly that answer. Wiring Cere is a separate change in home-servers (`leia/ansible/playbook.yml`, `cere_lan_ip`, then the nginx tag).

When Cere is wired, verify:

1. `launchctl list | grep com.nakomis.bau-api` shows the job, and `bau version` runs from `current/venv/bin/bau` (the API serves the packaged dashboard at `/`, and its footer shows the version).
2. On Cere: `curl -s localhost:8800/state.json | head -c 200` returns JSON, and `curl -sI localhost:8800/` shows `200`, `text/html`, `cache-control: no-cache` and a `content-security-policy` header.
3. Via Leia, with the client certificate: `/` returns the page (not the 503 text) and `/state.json` returns JSON.
4. In Chrome, the dashboard renders, the tier matches BD-1's BAU row, and the PR links open the right GitHub pull requests.

- The `proxy-token` keychain item (see "Keychain items") must exist before the API job is installed. If it is missing, the play skips `com.nakomis.bau-api` with a loud message and installs the other jobs; add the item and re-run the play.
- Abandon from the shell: `bau abandon REF [--note "why"]`. It restores the item's previous state first, then closes the PR, deletes the checkout, removes the `BAU` label and posts a `[bau:abandoned]` comment.
- Abandon takes the same lock as `bau run`. While a night is running it answers 409 ("another BAU operation is in progress; try again shortly"); try again after about 05:30.

## Logs

```bash
LOGS=~/Library/Logs/BAU
less +F $LOGS/bau-run.log          # the nightly run (stdout and stderr)
less +F $LOGS/bau-snapshot.log     # the hourly snapshot
ls $LOGS/                          # per-session logs: <date>-<REF>.log
docker logs bau-egress             # egress proxy: denied requests
```

## Upgrade and rollback

Do not upgrade while a run is active, roughly 01:00 to 05:30: the play reloads the jobs and would interrupt it. It skips itself inside that window or while a run holds the lock, unless run with `-e bau_force=true`. Use "Stop a run now" first if one is in progress.

Upgrading is a home-servers pull request bumping `bau_version` in `cere/ansible`, then `scripts/cere-ansible.sh --tags bau`, run through the midichlorian MCP. `-e bau_version=<v>` overrides the pin for a one-off.

Rollback is the same in reverse: set `bau_version` back and run the play. The three newest releases are kept under `releases/`, so a rollback is a symlink flip and a job reload, with no download or build. At the console, in an emergency:

```bash
cd ~/Library/"Application Support"/BAU
ln -sfn releases/<previous-version> current
launchctl kickstart -k gui/$(id -u)/com.nakomis.bau-api
```

The plists name `current`, so nothing else moves. Images are never deleted from Nexus (`ALLOW_ONCE`), so an old tag can always be pulled. Never delete a release the deployment tracker has recorded; ship a fixed one instead.

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.nakomis.bau-run
launchctl bootout gui/$(id -u)/com.nakomis.bau-snapshot
launchctl bootout gui/$(id -u)/com.nakomis.bau-api
rm ~/Library/LaunchAgents/com.nakomis.bau-{run,snapshot,api}.plist
docker compose -p bau down
```

The `rm` line stops launchd loading the jobs again at login. `$BAU_HOME` holds the bare clones and checkouts; remove it only when you mean to discard them.

## Adding a repository to BAU

1. In GitHub, open Settings → Applications → Installed GitHub Apps → **nakomis-bau** → Configure, and tick the repository under "Only select repositories".
2. Make sure `main` there is protected (a pull request is required and force pushes are blocked); BAU refuses unprotected repositories.
3. In ATC (`atc.plane.home.nakomis.com`, or the Plane MCP's `set_project_meta`), set the project's repository, whether it deploys and the deploy action, and turn on `bau_enabled`. BAU reads this at the start of each run.
4. Add `bau@nakomis.com` to that Plane project as a member.
