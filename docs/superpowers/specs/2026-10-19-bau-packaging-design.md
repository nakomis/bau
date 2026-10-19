# BAU packaging and installation (BAU-24)

Status: draft for Martin's review, 19 Oct 2026. Nothing here is implemented.
Amends section 3 (components), section 12 (hardening) and the deployment
notes of `2026-10-14-bau-design.md`; see "Changes to the existing spec" below.

## 1. Problem

BAU is deployed by cloning `nakomis/bau` to `~/bau/src` on Cere and running
`install.sh` there (`cere/ansible/tasks/bau.yml` in home-servers, HOME-451).
That has four faults:

- The host builds what it runs: `uv sync`, `pnpm build`, `docker build`, all on
  the machine that does the work, and `pnpm`, `uv` and `docker` all have to be
  present for an upgrade, not just for running.
- Nothing is versioned. "What is Cere running?" is a git SHA in a checkout that
  a person can edit. A dirty checkout already makes the play hold
  (`_bau_dirty_hold`), and pausing by editing `bau.yaml` dirties it.
- Config sits in the checkout (`~/bau/src/config`, hard-coded as
  `DEFAULT_CONFIG` in `bau/cli.py`), and `projects.yaml` is a hand-edited
  mirror of data that is moving to ATC.
- There is no rollback except `git checkout` plus a rebuild, which needs the
  network and a build toolchain at the worst moment.

The other Jedi do not work like this. BD-1 is built by CI, versioned by the
deployment tracker, published as a release and installed from it; the Rust
images are built by CI, tagged `<version>-<sha>` and pushed to
`registry.home.nakomis.com`. BAU should follow suit.

## 2. What CI builds

A new `release` job in `.github/workflows/ci.yml`, on push to `main` after the
`python` and `web` jobs pass (as `bd-1-release` needs `bd-1`). On pull requests
it builds everything and publishes nothing, as `publish-images` does.

### 2.1 Version

Computed exactly as bd-1 and the Rust images do it:

1. Assume the sandbox CI role, then
   `nakomis/nakomis-infra/.github/actions/compute-version@main` with
   `project: bau`, `environment: sandbox` and `version-file: version.json`
   (seeded from `pyproject.toml`, currently 0.1.0). The bump keyword comes from
   the merged PR's title and body, read through the commits API as `bd-1-release`
   does.
2. Never reuse a version: bump the patch while either the GitHub release
   `bau-v<version>` or any image tag `<version>-<sha>` exists (the registry is
   `ALLOW_ONCE`).
3. Stamp the result into the build, not the source: CI rewrites the
   `[project] version` in a throwaway copy of `pyproject.toml` and writes
   `bau/_build.py` (`VERSION`, `GIT_SHA`). `bau version` and the dashboard
   footer read it. A development checkout reports `0.0.0+local`.
4. After everything publishes, `record-deployment` with `environment: sandbox`
   (meaning "released", as for bd-1). `prod` means "installed on Cere" (section
   3.5).

### 2.2 Host artefact: one bundle, a wheel inside

A zipapp is not worth it: `PyJWT[crypto]` brings `cryptography`, a compiled
wheel, and the dashboard is a directory of static files. A wheel is the natural
unit. The release carries a **bundle**, `bau-<version>.tar.gz`, containing:

| File | Purpose |
|---|---|
| `bau-<version>-py3-none-any.whl` | The `bau` package, with `web/dist` force-included as package data (`bau/web_dist/`), and `config/bau.yaml` as `bau/defaults.yaml` |
| `requirements.txt` | `uv export --frozen --no-dev --hashes`: every dependency pinned and hashed from `uv.lock`, so Cere installs exactly what CI tested |
| `launchd/*.plist.in` | The four job definitions with `@BAU_HOME@` and `@HOME@` placeholders (section 4.3) |
| `compose.yml` | The egress proxy compose file, image tags taken from `.env` |
| `VERSION`, `SHA256SUMS` | Version, and checksums of the above |

Published as GitHub release `bau-v<version>` (the repo is public, so Cere needs
no credential to fetch it; same shape as `bd-1-v<version>`), with `gh release
create ... --target $GITHUB_SHA`. The wheel is built on `ubuntu-latest`: it is
pure Python.

### 2.3 Images, to Nexus

| Image | Built from |
|---|---|
| `registry.home.nakomis.com/nakomis/bau-agent` | `container/Dockerfile` |
| `registry.home.nakomis.com/nakomis/bau-egress` | `container/egress.Dockerfile`, with `tinyproxy.conf` and `filter` **baked in** (today they are bind-mounted from the checkout, which will not exist) |

Tag `<version>-<short sha>`, no `latest`, `--provenance=false --sbom=false`,
logged in with the same four secrets, copied onto `nakomis/bau`
(`NEXUS_DOCKER_USER`, `NEXUS_DOCKER_PASSWORD`, `NEXUS_CLIENT_CERT`,
`NEXUS_CLIENT_KEY`; docs/runbooks/nexus.md, "Publishing from CI"). Push only on
`main`.

**Architecture.** Cere is an M1 Mac and Colima runs arm64 Linux, so these are
`linux/arm64` images, built natively on `ubuntu-24.04-arm` (free for a public
repo), not under QEMU. The existing Rust images are `linux/amd64` for the
Intel Jedi; BAU's are the first arm64 ones in the registry.

`install.sh` (and its `docker build` / `docker compose up --build`) goes away.
The in-image Claude Code pin stays in the Dockerfile.

### 2.4 Order of publication

Images first, then the release (a release that points at missing images is the
bad half-state), then `record-deployment`. The release's `VERSION` file holds
the exact image refs. A failure before the release leaves orphan images, which
is harmless because tags are never reused.

## 3. How a Jedi installs and upgrades it

The existing pattern: Ansible in home-servers, playbook per Jedi, run from
phi with `scripts/cere-ansible.sh`. This replaces the "Checkout and install"
half of `cere/ansible/tasks/bau.yml`; the secrets, certificate, claude-CLI and
docker-compose preconditions in that file stay as they are.

### 3.1 Pinned version

`cere/ansible/vars/` gets `bau.vars.yml` (or a `bau_version` in the playbook
vars): `bau_version: 0.4.2`. Upgrading BAU is a home-servers PR bumping that
line, then `scripts/cere-ansible.sh --tags bau`. This matches
`packaging/aws-signing-helper.vars.yml` and makes "what runs on Cere" a
reviewed fact in git. `-e bau_version=0.4.1` overrides for a one-off or a
rollback.

### 3.2 Layout on Cere

```
~/Library/Application Support/BAU/
  releases/<version>/      venv/  launchd/  compose.yml  .env  VERSION
  current -> releases/<version>
  config/
    host.yaml              Ansible-rendered, the only config that differs per host
    cere.crt.pem  cere.key.pem   (0600; moved from ~/.config/bau)
  state/                   state.json  atc-cache.json  paused
  repos/  work/            bare clones and ticket checkouts, as today
~/Library/Logs/BAU/        bau-run.log, bau-api.log, ...
```

Nothing under `~/Library/Application Support/BAU` is a git repository, and
nothing is edited by hand: `host.yaml` and the certificates are Ansible's;
`state/paused` is touched by the dashboard or by Martin. The existing
`/Users/nakomis/bau/...` paths and `/Users/bau/...` in the original spec are
replaced by this.

### 3.3 Upgrade sequence (tag `bau`)

1. Preconditions, as today (claude CLI, keychain items, compose plugin).
2. **Hold gate, unchanged**: skip if a run is active or Cere's clock is
   inside 00:10-05:45, unless `-e bau_force=true`. The dirty-checkout hold is
   deleted, since there is no checkout.
3. If `releases/<bau_version>/` exists, go to step 8 (idempotent re-run).
4. Download `bau-v<version>` with `gh release download` (or `curl`, public repo)
   into a temp dir; verify `SHA256SUMS`.
5. `uv venv --python 3.12 releases/<v>/venv`, then
   `uv pip install --require-hashes -r requirements.txt <wheel>`. `uv` is
   already a bootstrap requirement of Cere and manages the Python; no `pnpm`.
6. `docker pull` the two images by the tags in `VERSION`; write `.env`.
7. Smoke test **before** switching: `releases/<v>/venv/bin/bau --config
   <config> run --dry-run` and `bau version`. Failure leaves `current` untouched.
8. Render `host.yaml` and the plists into `~/Library/LaunchAgents/`; flip the
   `current` symlink; `docker compose -p bau up -d` for the egress proxy;
   `launchctl bootout` then `bootstrap` for each job whose plist changed.
9. Prune all but the three newest releases (and image tags, via
   `docker image prune` on unused `bau-*`).
10. Record `prod` in the deployment tracker (section 3.5).

Because jobs reference `.../current/venv/bin/bau`, a running night keeps its
open files in the old release directory; the hold gate means we do not flip
mid-night anyway.

### 3.4 Python, `uv` and `docker` on the host

Homebrew `uv` and the Colima/docker CLI remain host prerequisites (asserted by
the play). `pnpm` and Node are no longer needed on Cere for BAU.

### 3.5 Recording the install

The tracker accepts only allow-listed CI roles, not Cere's own credentials, so
copy BD-1: a `bau-installed` workflow in `nakomis/bau` (`workflow_dispatch`
with `version` and `host`) that checks the release exists and calls
`record-deployment` with `project: bau, environment: prod`. The play triggers
it with `gh workflow run` at the end of a successful install (skipped quietly
if `gh` is unauthenticated, as `install.sh` does for BD-1). This also gives the
deferred "Deploy auto-close from the deployment tracker" ticket (original spec
section 16) a record to read for BAU's own deploys.

## 4. Where config lives

### 4.1 Principle

Never in a checkout, never hand-edited on the host. Two sources:

- **Policy**, in the wheel: `bau/defaults.yaml` (today's `config/bau.yaml`
  minus host bits). Changes by PR and release, so a rollback restores the policy
  that went with the code. This keeps the rule "policy constants live in a
  config file, never in code" while making the file part of the artefact.
- **Host facts**, in `config/host.yaml`, rendered by Ansible and deep-merged
  over the defaults.

`--config` becomes the directory holding `host.yaml`; the default is
`$BAU_HOME/config`, with `BAU_HOME` defaulting to
`~/Library/Application Support/BAU`. The hard-coded
`/Users/nakomis/bau/src/config` disappears. In a development checkout,
`uv run bau --config config/` still works and `config/host.example.yaml`
documents the overlay.

### 4.2 What remains of `bau.yaml` once projects come from ATC

`projects.yaml` is no longer read (ATC-7, PR #13) and is not shipped in the
bundle; the file is deleted in the packaging PR. `repo`, `deploys`,
`deploy_action` and `bau_enabled` live only in ATC's tables, read through
`bau/atc.py` with the cached copy as fallback.

Remaining, and where each goes:

| Key | Home |
|---|---|
| `schedule`, `limits`, `nudge`, `models` | `defaults.yaml` (policy) |
| `identities`, `github_app` (ids), keychain `service`/`account` refs | `defaults.yaml`; they are estate-wide, not per host |
| `plane.url`, `plane.workspace`, `atc.url` | `defaults.yaml` |
| `plane.client_cert`, `plane.client_key`, `paths.*` (repos, work, logs, snapshot, pause_file, atc_cache), `container.image` and `proxy` tags | `host.yaml`, rendered from `BAU_HOME` |
| `paused` | removed from YAML: the pause file (`state/paused`, BAU-18) is the only switch. The YAML cannot be edited on the host any more, and the dashboard and Martin already write the file |
| `container.image` | derived: the wheel's `_build.py` knows its own `<version>-<sha>`, so the agent image can never drift from the code |

`${BAU_HOME}` and `~` are expanded when paths load (`_path()` already
expands `~`), so no absolute `/Users/nakomis` remains in anything shipped.

### 4.3 launchd

The plist templates (`@BAU_HOME@`, `@HOME@`) are in the bundle, versioned with
the code, so schedule changes roll back with it. Ansible renders them
(a plain `replace`, not logic) into `~/Library/LaunchAgents`. Log paths move
to `~/Library/Logs/BAU`. The old `bau-sweep` clean-up that `install.sh` does is
carried by the play's migration step (section 5).

## 5. Migration from the checkout install

Once, in the same play, guarded by `~/bau/src` existing:

1. Hold gate as above.
2. `launchctl bootout` the four jobs; move `~/bau/{repos,work,logs}` and
   `state.json`, `atc-cache.json`, `paused` into the new tree; move
   `~/.config/bau/cere.*.pem`.
3. Install the pinned release and load the jobs.
4. Leave `~/bau/src` in place, renamed `~/bau/src.retired`, for one good night;
   a follow-up play task deletes it. The keychain items are untouched.

## 6. Rollback

- **Normal**: set `bau_version` back (or `-e bau_version=<previous>`) and
  run `--tags bau`. The previous release directory is retained (last three), so
  step 3 skips the download and the install; it is a symlink flip, a `.env`
  re-point (images are already local) and a job reload. No network, no build.
- **Emergency, at the console**: `ln -sfn releases/<v> current` and
  `launchctl kickstart`; the plists name `current`, so nothing else moves.
- **Images**: `ALLOW_ONCE` means an old tag is always still in Nexus.
- **State**: `state.json` and `atc-cache.json` are read tolerantly (unknown
  keys ignored, missing keys defaulted), so a release may be rolled back one
  version without a state migration. A change that breaks that must say so in
  its PR and bump the minor.
- **Bad release**: ship a fixed `bau-v<next>`; never delete a release the tracker
  has recorded.

## 7. Changes to the existing spec

Edit `2026-10-14-bau-design.md` in the implementation PR (not this one):

- Section 3 table: "Repos folder" and "Ticket checkouts" paths become
  `<BAU_HOME>/repos` and `<BAU_HOME>/work`.
- Section 7 ("Repo resolution"): `projects.yaml` is replaced by ATC
  (already noted for ATC-7); section 12's "alongside the repo's line in
  `config/projects.yaml`" likewise.
- Section 12: "a rebuilt Cere with the repo and the credentials carries on"
  becomes "with the playbook and the credentials": the release is fetched, not
  cloned.
- New "Deployment" text: CI-built bundle and images, Ansible pinned version,
  config split, rollback. `install.sh` and the README/runbook install steps are
  deleted.
- BAU's own ATC row becomes `deploys: true`, `deploy_action: "Bump bau_version
  in home-servers cere/ansible and run scripts/cere-ansible.sh --tags bau"`, so
  BAU's merged PRs land on its own deploy list.
- Section 16: "Deploy auto-close" gains BAU as its first tracked project;
  "Host-side deploy verification" is partly superseded for BAU by section 3.5.

Also in home-servers: `cere/ansible/tasks/bau.yml` loses the checkout and
install.sh steps, `cere/CLAUDE.md` and the BAU part of
`docs/runbooks/ec2-dr/cere.md` change, and `registry.home.nakomis.com`
`certs.d` is laid down on Cere (the runbook notes the Leia playbook only does
`docker.home.nakomis.com` today; Cere already holds CN=cere).

## 8. Prerequisites outside this repo

- The `nakomis-infra` tracker allows `nakomis/bau` to assume the sandbox CI
  role, and a `bau` project exists in the tracker.
- Four Nexus secrets on `nakomis/bau`; a dedicated client certificate for it is
  cleaner than reusing home-infra's, so revocation is independent.
- A BAU Plane work item per phase (section 9).

## 9. Suggested phases

1. In bau: `defaults.yaml` plus `host.yaml` overlay, `BAU_HOME`, `_build.py`,
   package the dashboard as package data, drop `projects.yaml` and the
   hard-coded paths. Testable with no CI change.
2. In bau: CI `release` job, Dockerfiles with the proxy config baked in, the
   `bau-installed` workflow.
3. In home-servers: the Ansible install, migration and rollback tasks; pin
   `bau_version`; first release installed by hand with `-e bau_force=true`.
4. Retire `install.sh`, `~/bau/src.retired`; update the spec and docs.

## 10. Open questions for Martin

1. **Where does the host bundle live: GitHub release or Nexus `artefacts-raw`?**
   Recommend the GitHub release. The repo is public, BD-1 does it that way and
   Cere needs no credential to fetch it. Nexus is for images and for artefacts
   that a repo cannot publish itself (the signing helper).
2. **Tracker versions or plain git tags?** Recommend the tracker, for consistency
   with BD-1 and the Rust images, and because it is what the planned deploy
   auto-close reads. Cost: the allow-list change in section 8.
3. **Who owns the plists: the bundle or Ansible?** Recommend the bundle (templates
   in this repo, rendered by Ansible) so the schedule moves and rolls back with
   the code. The alternative, Ansible templates, needs two PRs for a time change.
4. **Is a path with a space acceptable?** `~/Library/Application Support/BAU`
   needs quoting in plists, compose bind mounts and the agent's mounts.
   Recommend yes, as you suggested it, but with a phase-1 test of a bind mount
   from that path through Colima first. The fallback is `~/.local/share/bau`.
5. **Should everything move there, including `repos/` and `work/`?** These are
   data, possibly large, and are not backed up. Recommend yes (one root, one
   `BAU_HOME`), with logs in `~/Library/Logs/BAU`.
6. **Pin `bau_version` in home-servers, or let Cere track the latest release?**
   Recommend the pin: an upgrade is a reviewed PR and a rollback is a revert.
   A `-e bau_version=latest` escape for testing is cheap.
7. **Record `prod` installs in the tracker now?** Recommend yes, the
   `bau-installed` workflow is a copy of the BD-1 one and BAU's own deploy row
   then closes itself when the auto-close ticket lands. Defer only if you want
   phase 3 smaller.
8. **arm64-only images?** Recommend yes: Cere is the only consumer and a native
   arm64 runner is free for a public repo. Say so if a Jedi on Intel should
   ever run BAU, and we add `linux/amd64` then.
9. **Public repo, Nexus push secrets.** Pushing from `main` only, using GitHub
   environment-scoped secrets, is what I recommend. The alternative is building
   images on Cere's own runner, which brings back a host build.
10. **Keep `projects.yaml` in the repo until the first good night on ATC?** PR #13
    says it stays as the seed for ATC's import. Recommend it is not shipped in the
    bundle from the first release, and is deleted from the repo once ATC has run
    a night.
