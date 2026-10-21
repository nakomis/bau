# BAU Packaging Implementation Plan (BAU-24, phases 1 and 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** BAU is built, versioned and published by CI (a GitHub release bundle plus two arm64 images in Nexus), with policy in the wheel and host facts in an Ansible-rendered overlay, so that Cere installs a release instead of building a checkout.

**Architecture:** Phase 1 splits config (`bau/defaults.yaml` in the package, `host.yaml` overlay under `BAU_HOME`), stamps the version into the build, ships the dashboard as package data and turns the plists into templates. Phase 2 adds `scripts/build-bundle.sh`, a `release` pipeline in `ci.yml` (version, images, bundle, tracker) and a `bau-installed` workflow. Phase 3 (the Ansible install in home-servers) and phase 4 (retirement) are separate plans; nothing here touches Cere.

**Tech Stack:** Python 3.12, uv, hatchling, PyYAML, FastAPI; React/Vite (pnpm); GitHub Actions; docker buildx on `ubuntu-24.04-arm`; nakomis-infra `compute-version` / `record-deployment` actions.

**Spec:** `docs/superpowers/specs/2026-10-19-bau-packaging-design.md`. Martin accepted every recommendation in its section 10 on 21 Oct 2026; the decisions below follow them.

## Global Constraints

- British English in code, comments, docs and commit messages. No attribution lines in commits or PRs.
- Branch `bau-24-packaging`; every commit message ends `(BAU-24)`; PR title `feat: CI-built releases and a config overlay (BAU-24)`.
- `uv run pytest` keeps the 70% coverage floor; `uv run ruff check .` and `uv run ruff format --check .` stay clean; `pnpm lint && pnpm test` in `web/` stay clean.
- `BAU_HOME` defaults to `~/Library/Application Support/BAU` (decision 4; fallback `~/.local/share/bau` only if prerequisite P2 fails). Logs default to `~/Library/Logs/BAU` (decision 5).
- Nothing shipped contains `/Users/nakomis`: paths are written with `${BAU_HOME}`, `~` or the `@BAU_HOME@` / `@HOME@` placeholders.
- Images: `registry.home.nakomis.com/nakomis/bau-agent` and `registry.home.nakomis.com/nakomis/bau-egress`, tag `<version>-<7-char sha>`, never `latest`, `linux/arm64` only (decision 8), `--provenance=false --sbom=false`, pushed only on push to `main` (decision 9).
- Release tag `bau-v<version>`, asset `bau-<version>.tar.gz` (decision 1). Tracker project `bau`: `sandbox` = released, `prod` = installed (decisions 2, 7).
- Publication order: images, then the release, then `record-deployment`.
- `projects.yaml` is deleted; projects come only from ATC (decision 10).
- Third-party action majors: `actions/checkout@v7`, `astral-sh/setup-uv@v6`, `pnpm/action-setup@v6`, `actions/setup-node@v7`, `aws-actions/configure-aws-credentials@v6`, `actions/upload-artifact@v7`, `actions/download-artifact@v8`, `docker/setup-buildx-action@v3`.

## Review Focus

1. **The space in `BAU_HOME`.** Every consumer must keep `~/Library/Application Support/BAU/...` as one argument: the `docker run -v` argv, `run_lock`, plist strings, `compose`. Pinned by `test_session_mounts_a_path_with_a_space_as_one_argument` (Task 2) and `test_rendered_plists_keep_the_space` (Task 4).
2. **A host overlay that is partial or wrong.** A `host.yaml` that sets one nested key must not wipe its siblings, and an unknown top-level key must be an error, not silently ignored. Pinned by `test_overlay_merges_nested_keys` and `test_unknown_host_key_is_an_error` (Task 2).
3. **The run lock moving with the logs.** Today the lock is `paths.logs.parent / "run.lock"`; with logs in `~/Library/Logs/BAU` that would put it in `~/Library/Logs`. It moves to `paths.lock`, and `bau run`, `bau abandon` and the API must all use the same file. Pinned by `test_every_lock_user_agrees` (Task 2).
4. **`--require-hashes` and the local wheel.** `uv pip install --require-hashes -r requirements.txt <wheel>` refuses an unhashed local wheel. The bundle's documented install is two steps (hashed requirements, then the wheel with `--no-deps`), and CI performs exactly that install before publishing. Pinned by the `bundle-smoke` step (Task 6) and `test_bundle_installs_and_reports_its_version` (Task 5).
5. **A re-run of the same commit.** A re-run must neither fail on `ALLOW_ONCE` nor publish a second version for one commit's images. The image push is skipped when `<version>-<sha>` already exists; the release loop bumps past existing `bau-v<version>` tags. Pinned in Task 6 step 3's `docker manifest inspect` guard and the existing-release loop.

---

## Before Task 1: prerequisites (controller, not implementers)

These sit outside `nakomis/bau`. Do them in this order, recording each in the ledger.

- [ ] **P1. Pin Cere to its current BAU commit (home-servers PR).** Cere's play checks out `bau_ref: main` (`cere/ansible/playbook.yml:178`) into an editable venv, so any Cere play run after Task 2 merges would run the new code against the old config and break the night. Open a home-servers PR on branch `bau-24-pin-cere` setting `bau_ref` to the full SHA Cere runs now (`ssh cere git -C ~/bau/src rev-parse HEAD`; `d7ac008…` on 21 Oct), with a comment naming BAU-24 phase 3 as the change that removes it. No playbook run is needed: the pin only matters on the next run. Merge after an independent review **before** this plan's PR merges.
- [ ] **P2. Colima bind mount through a space.** On Cere (a read-only probe, not a playbook):
  ```bash
  ssh cere 'mkdir -p "$HOME/Library/Application Support/BAU-probe" && echo ok > "$HOME/Library/Application Support/BAU-probe/f" &&
    DOCKER_HOST=unix://$HOME/.colima/default/docker.sock docker run --rm -v "$HOME/Library/Application Support/BAU-probe:/p" alpine:3.22 cat /p/f;
    rm -rf "$HOME/Library/Application Support/BAU-probe"'
  ```
  Expected: `ok`. If it fails, change the `BAU_HOME` default in Global Constraints to `~/.local/share/bau` before dispatching Task 2, and ledger the ruling.
- [ ] **P3. A sandbox CI role for `nakomis/bau`, allow-listed by the tracker.** `nakomis/bau` has no CDK of its own. Open a nakomis-infra PR that adds role `nakomis-bau-github-ci-sandbox` (trust: GitHub OIDC, both subject forms `repo:nakomis@1488244/bau@<repo id>:*` and `repo:nakomis/bau:*`; permissions: whatever `compute-version` and `record-deployment` need, as for the roles already allow-listed) and adds it to the tracker's allow-list, following nakomis-infra #30. Merge, deploy to prod as #30 was, then set the repo variable `AWS_SANDBOX_ROLE_ARN` on `nakomis/bau` to the role's ARN.
- [ ] **P4. Release environment and Nexus secrets.** Create GitHub environment `release` on `nakomis/bau` with a deployment branch policy allowing `main` only, and set `NEXUS_DOCKER_USER`, `NEXUS_DOCKER_PASSWORD`, `NEXUS_CLIENT_CERT` and `NEXUS_CLIENT_KEY` as **environment** secrets on it (decision 9). The client cert is a dedicated one (spec section 8): ask Martin to issue `ci-bau` from the cert portal, as he did for ATC; the Nexus user is a new `ci-bau` with push rights on the docker-hosted repo, created with the admin password from SSM `/nexus/admin-password`. Never print a secret.
- [ ] **P5. Plane items.** BAU-24 carries this plan. Create one BAU item each for phase 3 (home-servers Ansible install, migration, rollback; 2 points) and phase 4 (retire the checkout, docs; 1 point), with `create_work_item` and `points`.

P3 and P4 block only Task 6's publishing steps. Tasks 1 to 5 may proceed while they are outstanding.

---

### Task 1: Build stamp and `bau version`

**Files:**
- Create: `bau/version.py`
- Modify: `bau/cli.py` (new `version` subcommand), `.gitignore` (add `bau/_build.py`)
- Test: `tests/test_version.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `bau.version.VERSION: str`, `bau.version.GIT_SHA: str` (7 characters, or `""`), `bau.version.image_tag() -> str | None` (`f"{VERSION}-{GIT_SHA}"`, or `None` when unstamped). `bau/_build.py`, when present, defines `VERSION` and `GIT_SHA` as string constants and nothing else; Task 5 writes it.

- [ ] **Step 1: Write the failing tests**

```python
def test_unstamped_checkout_reports_local(monkeypatch):
    monkeypatch.setitem(sys.modules, "bau._build", None)  # import fails
    v = importlib.reload(bau.version)
    assert (v.VERSION, v.GIT_SHA, v.image_tag()) == ("0.0.0+local", "", None)

def test_stamped_build_reports_its_stamp(monkeypatch):
    monkeypatch.setitem(sys.modules, "bau._build", types.SimpleNamespace(VERSION="0.4.2", GIT_SHA="abc1234"))
    v = importlib.reload(bau.version)
    assert v.image_tag() == "0.4.2-abc1234"

def test_version_command_needs_no_config(capsys):  # tests/test_cli.py
    assert main(["--config", "/nonexistent", "version"]) == 0
    assert capsys.readouterr().out.startswith("bau ")
```

- [ ] **Step 2: Run them:** `uv run pytest tests/test_version.py tests/test_cli.py -k version -q`. Expected: FAIL (no module `bau.version`, no `version` command).
- [ ] **Step 3: Implement.** `bau/version.py` imports `bau._build` in `try/except ImportError`. In `cli.py`, add subparser `version` and dispatch it in `main` **before** `load_config`, printing `bau <VERSION> (<GIT_SHA or "unstamped">)`. Add `bau/_build.py` to `.gitignore`.
- [ ] **Step 4: Run:** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit:** `feat: build stamp and bau version (BAU-24)`

### Task 2: Config split, `BAU_HOME`, ATC-only projects

**Files:**
- Move: `config/bau.yaml` → `bau/defaults.yaml` (with `git mv`, then edit)
- Create: `config/host.example.yaml`
- Delete: `config/projects.yaml`
- Modify: `bau/config.py`, `bau/cli.py`, `bau/api.py`, `bau/runloop.py`
- Test: `tests/test_config.py`, `tests/test_cli.py`, `tests/test_session.py`, `tests/test_api.py`, `tests/test_container_files.py`

**Interfaces:**
- Consumes: `bau.version.image_tag()` (Task 1).
- Produces:
  - `bau.config.bau_home() -> Path`: `$BAU_HOME`, expanded, else `~/Library/Application Support/BAU`.
  - `bau.config.load_config(config_dir: Path) -> Config`: packaged `defaults.yaml`, deep-merged with `config_dir / "host.yaml"` when that exists. Raises `ValueError` naming any top-level key in `host.yaml` that `defaults.yaml` lacks. Expands `${BAU_HOME}` (to `bau_home()`) and `~` in every `paths.*`, `plane.client_cert` and `plane.client_key`.
  - `Paths` gains `lock: Path`. `Config` loses `paused`.
  - `paused_reader(pause_file: Path) -> Callable[[], bool]`: the pause file only.
  - `cli.DEFAULT_CONFIG` is removed; `--config` defaults to `bau_home() / "config"`.
  - `ProjectConfig` stays in `bau/config.py` (ATC uses it); `load_projects` is deleted.

`bau/defaults.yaml` is today's `bau.yaml` minus `paused` and the host-specific values below. Its `paths` read:

```yaml
paths:
  repos: ${BAU_HOME}/repos
  work: ${BAU_HOME}/work
  logs: ~/Library/Logs/BAU
  snapshot: ${BAU_HOME}/state/state.json
  pause_file: ${BAU_HOME}/state/paused
  atc_cache: ${BAU_HOME}/state/atc-cache.json
  lock: ${BAU_HOME}/state/run.lock
```

`plane.client_cert` and `plane.client_key` become `${BAU_HOME}/config/cere.crt.pem` and `${BAU_HOME}/config/cere.key.pem`. `container.image` is **derived** when `host.yaml` does not set it: `registry.home.nakomis.com/nakomis/bau-agent:<image_tag()>` for a stamped build, `bau-agent:latest` for an unstamped one. The defaults file has no `container.image` key. `host.yaml` may set it as an override. Replace the header comment about pausing with one pointing at `paths.pause_file`.

`config/host.example.yaml` documents the overlay with the keys Ansible will render (`plane.client_cert`, `plane.client_key`, `paths.*`), all commented as optional overrides.

- [ ] **Step 1: Write the failing tests** (in `tests/test_config.py`; replace the `load_projects`, `projects.yaml` and YAML-`paused` tests, which go away with the code they cover):

```python
def test_defaults_alone_load(tmp_path, monkeypatch):
    monkeypatch.setenv("BAU_HOME", str(tmp_path / "Application Support" / "BAU"))
    cfg = load_config(tmp_path)  # no host.yaml
    assert cfg.paths.snapshot == tmp_path / "Application Support" / "BAU" / "state" / "state.json"
    assert cfg.paths.lock.name == "run.lock" and cfg.paths.lock.parent == cfg.paths.snapshot.parent
    assert cfg.paths.logs == Path("~/Library/Logs/BAU").expanduser()

def test_overlay_merges_nested_keys(tmp_path):
    (tmp_path / "host.yaml").write_text("limits:\n  session_budget_usd: 9.5\n")
    cfg = load_config(tmp_path)
    assert cfg.limits.session_budget_usd == 9.5
    assert cfg.limits.too_big_files == 15  # sibling kept from the defaults

def test_unknown_host_key_is_an_error(tmp_path):
    (tmp_path / "host.yaml").write_text("pathz:\n  repos: /x\n")
    with pytest.raises(ValueError, match="pathz"):
        load_config(tmp_path)

def test_container_image_follows_the_build_stamp(tmp_path, monkeypatch):
    monkeypatch.setattr("bau.config.image_tag", lambda: "0.4.2-abc1234")
    assert load_config(tmp_path).container.image == "registry.home.nakomis.com/nakomis/bau-agent:0.4.2-abc1234"
    monkeypatch.setattr("bau.config.image_tag", lambda: None)
    assert load_config(tmp_path).container.image == "bau-agent:latest"

def test_host_yaml_may_override_the_image(tmp_path):
    (tmp_path / "host.yaml").write_text("container:\n  image: bau-agent:dev\n")
    assert load_config(tmp_path).container.image == "bau-agent:dev"

def test_nothing_shipped_names_a_home_directory():
    text = (Path(bau.__file__).parent / "defaults.yaml").read_text()
    assert "/Users/" not in text and "paused:" not in text

def test_paused_reader_is_the_pause_file_only(tmp_path): ...
    # absent -> False; present (with or without a reason) -> True; reason logged once, as today
```

Plus, in the named files:
- `test_every_lock_user_agrees` (`tests/test_cli.py` or `tests/test_api.py`): with a config whose `paths.lock` is `tmp_path / "state" / "run.lock"` and `paths.logs` elsewhere, `bau run`'s lock, `bau abandon`'s `lock_path` and the API's `lock_path` are all `cfg.paths.lock`. Use the existing fakes; assert on the path each one receives.
- `test_session_mounts_a_path_with_a_space_as_one_argument` (`tests/test_session.py`): with `paths.work` under `tmp_path / "Application Support"`, the built `docker run` argv contains `f"{checkout}:{checkout}"` as a single element.
- `test_default_config_dir_is_under_bau_home` (`tests/test_cli.py`): with `BAU_HOME` set, `build_parser().parse_args(["run"]).config == Path(BAU_HOME) / "config"`.
- Update `tests/test_container_files.py::test_compose_network_name_matches_config` to read `bau/defaults.yaml`. Update `test_real_bau_yaml_points_at_atc` likewise.

- [ ] **Step 2: Run:** `uv run pytest -q`. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement.** Load the defaults with `importlib.resources.files("bau") / "defaults.yaml"`. Deep merge: dicts merge recursively, and any other value in `host.yaml` replaces. Expand `${BAU_HOME}` with a plain string replace before `expanduser`. `parse_args` must evaluate `bau_home()` at parse time, not import time, so the test's `monkeypatch.setenv` takes effect. Replace every `cfg.paths.logs.parent / "run.lock"` with `cfg.paths.lock`. Replace every `paused_reader(args.config / "bau.yaml", cfg.paused, …)` with `paused_reader(cfg.paths.pause_file)`. `Night`'s fallback becomes `lambda: False`. `bau run` creates `cfg.paths.lock.parent` and `cfg.paths.logs` if missing (`mkdir(parents=True, exist_ok=True)`) before taking the lock.
- [ ] **Step 4: Run:** `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`. Expected: PASS, coverage ≥ 70%.
- [ ] **Step 5: Commit:** `feat: policy in the package, host overlay under BAU_HOME, ATC-only projects (BAU-24)`

### Task 3: The dashboard as package data, and its version

**Files:**
- Modify: `bau/cli.py` (`serve`), `bau/api.py` (`/state.json`), `web/src/components/Footer.tsx` and the state type and fetch in `web/src` that `/state.json` feeds
- Test: `tests/test_api.py`, `tests/test_cli.py`, `web/src/components/Footer.test.tsx`

**Interfaces:**
- Consumes: `bau.version.VERSION`, `bau.version.GIT_SHA` (Task 1).
- Produces: `bau.api.static_dir() -> Path | None`. It returns the packaged `bau/web_dist` when that is a directory, else the checkout's `web/dist` (`Path(bau.__file__).parent.parent / "web" / "dist"`) when that is a directory, else `None`. `/state.json` responses gain `"version": {"version": str, "sha": str}`, added by the API at response time and never written to the snapshot file. Task 5 places the built dashboard at `bau/web_dist/` inside the wheel.

- [ ] **Step 1: Write the failing tests**

```python
def test_static_dir_prefers_the_packaged_copy(tmp_path, monkeypatch): ...
    # packaged bau/web_dist present -> it; absent but web/dist present -> that; neither -> None

def test_state_json_carries_the_version(client):
    body = client.get("/state.json").json()
    assert body["version"] == {"version": bau.version.VERSION, "sha": bau.version.GIT_SHA}
```

```tsx
it("shows the version from state.json", () => {
  render(<Footer version={{ version: "0.4.2", sha: "abc1234" }} />);
  expect(screen.getByText("Nakomis BAU 0.4.2 (abc1234)")).toBeInTheDocument();
});
it("shows just the name before state.json arrives", () => {
  render(<Footer />);
  expect(screen.getByText("Nakomis BAU")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run:** `uv run pytest tests/test_api.py -q` and `cd web && pnpm test`. Expected: FAIL.
- [ ] **Step 3: Implement.** `serve` passes `static_dir()` to `create_app` instead of `Path(args.config).parent / "web" / "dist"`. `Footer` takes an optional `version` prop, and the page that already holds the fetched state passes it down.
- [ ] **Step 4: Run** both suites plus `pnpm lint`. Expected: PASS.
- [ ] **Step 5: Commit:** `feat: serve the packaged dashboard and show the version (BAU-24)`

### Task 4: launchd templates and a release-ready compose file

**Files:**
- Move: `launchd/com.nakomis.bau-{run,snapshot,api}.plist` → `launchd/com.nakomis.bau-{run,snapshot,api}.plist.in`
- Modify: `container/compose.yml`, `container/egress.Dockerfile`
- Test: `tests/test_container_files.py`

**Interfaces:**
- Produces:
  - Three templates using exactly two placeholders, `@BAU_HOME@` and `@HOME@`, substituted by plain text replacement (Ansible's `replace` in phase 3).
  - `compose.yml` reads `BAU_EGRESS_IMAGE` from the environment or `.env`. Task 5's `VERSION` file supplies it.

There are three jobs, not the four the spec mentions: `bau-sweep` was folded into `bau-run` before this plan. Each template:
- runs `@BAU_HOME@/current/venv/bin/bau <cmd>`;
- logs to `@HOME@/Library/Logs/BAU/bau-<job>.log`;
- puts `@HOME@/.local/bin` first in `PATH`, then the existing Homebrew and system entries;
- sets `DOCKER_HOST` to `unix://@HOME@/.colima/default/docker.sock`;
- adds `BAU_HOME` = `@BAU_HOME@` to `EnvironmentVariables`.

Schedules and `KeepAlive` stay as they are. `compose.yml` drops `build:` and the two bind mounts and sets `image: ${BAU_EGRESS_IMAGE:?set BAU_EGRESS_IMAGE (the release's VERSION file)}`. `egress.Dockerfile` gains `COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf` and `COPY filter /etc/tinyproxy/filter`, with the build context `container/`.

- [ ] **Step 1: Write the failing tests** (replace the plist and `install.sh` tests in `tests/test_container_files.py`; leave `install.sh` itself for phase 4, because Cere is pinned to the old commit by P1):

```python
def render(name, bau_home="/Users/x/Library/Application Support/BAU", home="/Users/x"):
    text = (ROOT / "launchd" / f"com.nakomis.{name}.plist.in").read_text()
    return plistlib.loads(text.replace("@BAU_HOME@", bau_home).replace("@HOME@", home).encode())

def test_rendered_plists_keep_the_space():
    for name in ("bau-run", "bau-snapshot", "bau-api"):
        p = render(name)
        assert p["ProgramArguments"][0] == "/Users/x/Library/Application Support/BAU/current/venv/bin/bau"
        assert p["EnvironmentVariables"]["BAU_HOME"] == "/Users/x/Library/Application Support/BAU"
        assert p["StandardOutPath"] == f"/Users/x/Library/Logs/BAU/{name}.log"

def test_templates_name_no_home_and_no_other_placeholder():
    for f in (ROOT / "launchd").glob("*.plist.in"):
        text = f.read_text()
        assert "/Users/" not in text
        assert set(re.findall(r"@[A-Z_]+@", text)) <= {"@BAU_HOME@", "@HOME@"}

def test_egress_config_is_baked_into_the_image():
    df = (ROOT / "container" / "egress.Dockerfile").read_text()
    assert "COPY tinyproxy.conf" in df and "COPY filter" in df
    svc = yaml.safe_load((ROOT / "container" / "compose.yml").read_text())["services"]["bau-egress"]
    assert "volumes" not in svc and "build" not in svc
    assert svc["image"].startswith("${BAU_EGRESS_IMAGE")
```

Keep `test_run_plist`, `test_snapshot_plist`, `test_every_plist_path_has_claude_and_homebrew`, the network tests and the alpine pin test working against the rendered templates. Update `test_egress_image_built_from_pinned_alpine_and_no_latest` so that it checks the Dockerfile's `FROM` and no longer checks the compose `image`.

- [ ] **Step 2: Run:** `uv run pytest tests/test_container_files.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the moves and edits above.
- [ ] **Step 4: Run:** `uv run pytest -q`, then `for f in launchd/*.plist.in; do sed 's#@BAU_HOME@#/tmp/a b#g;s#@HOME@#/tmp#g' "$f" | plutil -lint -; done`. Expected: PASS, and `OK` from each lint.
- [ ] **Step 5: Commit:** `feat: launchd templates and baked-in egress config (BAU-24)`

### Task 5: `scripts/build-bundle.sh`

**Files:**
- Create: `scripts/build-bundle.sh` (executable)
- Test: `tests/test_bundle.py`

**Interfaces:**
- Consumes: `bau/_build.py` contract (Task 1); `bau/web_dist` location (Task 3); templates and compose (Task 4).
- Produces: `scripts/build-bundle.sh <version> <sha7> <outdir>` writes `<outdir>/bau-<version>.tar.gz` and prints its path. The script assumes `web/dist` is already built and refuses (exit 1, message naming `web/dist`) if it is missing. The tarball has one top-level directory, `bau-<version>/`, containing:
  - `bau-<version>-py3-none-any.whl`
  - `requirements.txt`
  - `launchd/com.nakomis.bau-{run,snapshot,api}.plist.in`
  - `compose.yml`
  - `INSTALL` (the two install commands below, verbatim)
  - `VERSION` (below)
  - `SHA256SUMS`, covering every other file in the directory, in `shasum -a 256` format

`VERSION` is `KEY=VALUE` lines, readable both by `source` and as a compose `.env`:

```
BAU_VERSION=<version>
BAU_SHA=<sha7>
BAU_AGENT_IMAGE=registry.home.nakomis.com/nakomis/bau-agent:<version>-<sha7>
BAU_EGRESS_IMAGE=registry.home.nakomis.com/nakomis/bau-egress:<version>-<sha7>
```

`INSTALL` (Review Focus 4: the wheel cannot sit in a `--require-hashes` install):

```
uv venv --python 3.12 venv
uv pip install --python venv/bin/python --require-hashes -r requirements.txt
uv pip install --python venv/bin/python --no-deps bau-<version>-py3-none-any.whl
```

The source checkout is never modified. The script works in a `mktemp -d` copy of `pyproject.toml`, `uv.lock`, `README.md`, `LICENSE` and `bau/`, without `.gitignore`. In that copy it:
1. rewrites `[project] version` to `<version>`;
2. writes `bau/_build.py`;
3. copies `web/dist` to `bau/web_dist`;
4. runs `uv build --wheel --out-dir`;
5. runs `uv export --frozen --no-dev --no-emit-project --hashes > requirements.txt`.

- [ ] **Step 1: Write the failing test** (marked `@pytest.mark.skipif(shutil.which("uv") is None, …)`; it creates a stub `web/dist/index.html` in a temp copy of the repo if the real one is absent):

```python
def test_bundle_installs_and_reports_its_version(tmp_path):
    out = run(["scripts/build-bundle.sh", "9.9.9", "abc1234", str(tmp_path)])
    with tarfile.open(tmp_path / "bau-9.9.9.tar.gz") as t:
        names = set(t.getnames())
        t.extractall(tmp_path, filter="data")
    d = tmp_path / "bau-9.9.9"
    assert {f"bau-9.9.9/{n}" for n in ("VERSION", "SHA256SUMS", "requirements.txt", "compose.yml", "INSTALL")} <= names
    assert "BAU_AGENT_IMAGE=registry.home.nakomis.com/nakomis/bau-agent:9.9.9-abc1234" in (d / "VERSION").read_text()
    run(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=d)
    for line in (d / "INSTALL").read_text().splitlines():
        run(["bash", "-c", line], cwd=d)
    assert run([str(d / "venv/bin/bau"), "version"]) == "bau 9.9.9 (abc1234)\n"
    assert (next((d / "venv/lib").glob("python3.12/site-packages/bau/web_dist/index.html"))).exists()
    assert not (Path.cwd() / "bau" / "_build.py").exists()  # checkout untouched

def test_bundle_refuses_without_a_built_dashboard(tmp_path): ...
    # in a temp repo copy with no web/dist: non-zero exit, stderr names web/dist, no tarball
```

- [ ] **Step 2: Run:** `uv run pytest tests/test_bundle.py -q`. Expected: FAIL (no script).
- [ ] **Step 3: Implement** with `set -euo pipefail`. Quote every path.
- [ ] **Step 4: Run:** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit:** `feat: build-bundle script (BAU-24)`

### Task 6: CI release pipeline and `bau-installed`

**Files:**
- Modify: `.github/workflows/ci.yml`
- Create: `.github/workflows/bau-installed.yml`, `version.json` (`{"version": "0.1.0"}`, from `pyproject.toml`)
- Test: `tests/test_workflows.py`

**Interfaces:**
- Consumes: `scripts/build-bundle.sh` (Task 5); P3's `vars.AWS_SANDBOX_ROLE_ARN`; P4's `release` environment secrets.
- Produces: the release `bau-v<version>` with asset `bau-<version>.tar.gz`; images `bau-agent` and `bau-egress` at `<version>-<sha7>`; tracker records `bau`/`sandbox` per release and `bau`/`prod` per install.

The new jobs in `ci.yml`. Every one of them runs on PRs too, but on a PR it publishes nothing.

1. **`version`** (`ubuntu-latest`; needs `python`, `web`):
   - On push to `main`: take the merged PR text as `bd-1-release` does (`gh api repos/$REPO/commits/$SHA/pulls`), assume `vars.AWS_SANDBOX_ROLE_ARN`, then run `nakomis/nakomis-infra/.github/actions/compute-version@main` with `project: bau`, `environment: sandbox`, `version-file: version.json`. Bump the patch while `gh release view "bau-v$v"` succeeds.
   - On PRs: output `0.0.0-pr<number>`.
   - Outputs: `version`, and `sha` (first 7 characters of `CHECKOUT_REF`'s commit).
2. **`images`** (`ubuntu-24.04-arm`; needs `version`; matrix `{name: bau-agent, dockerfile: container/Dockerfile}` and `{name: bau-egress, dockerfile: container/egress.Dockerfile}`; context `container`; `environment: release` only when publishing):
   - Always build with `docker buildx build --platform linux/arm64 --provenance=false --sbom=false`.
   - On push to `main`: first fail early if a secret is empty (copy home-servers' "Check the publishing secrets are set" step). Then lay the client cert into `/etc/docker/certs.d/registry.home.nakomis.com/` and log in.
   - Skip the push if `docker manifest inspect registry.home.nakomis.com/nakomis/<name>:<version>-<sha>` succeeds (Review Focus 5); otherwise push with `--push`.
3. **`release`** (`ubuntu-latest`; needs `version`, `images`, and `web` for `web/dist`, which the `web` job uploads as artefact `web-dist` with `actions/upload-artifact@v7`; `permissions: contents: write, id-token: write, pull-requests: read`):
   - Download `web-dist` into `web/dist`.
   - Run `scripts/build-bundle.sh`.
   - **`bundle-smoke`:** extract the tarball, run its `INSTALL` lines, and assert `venv/bin/bau version` prints `bau <version> (<sha>)`.
   - Then, only on push to `main`: `gh release create "bau-v$VERSION" "$TARBALL" --target "$GITHUB_SHA" --title "BAU $VERSION" --notes "<INSTALL contents and the two image refs>"`. Write `version.json` with the published version, then run `record-deployment@main` with `project: bau`, `environment: sandbox`, `version-file: version.json`.
4. **`ci-status`** needs `version`, `images` and `release` as well. It passes when each is `success` or `skipped`.

`bau-installed.yml` copies home-servers' `bd-1-installed.yml`, with these changes:
- the release name is `bau-v$VERSION`;
- it checks out `bau-v${{ inputs.version }}`;
- the role is `${{ vars.AWS_SANDBOX_ROLE_ARN }}`;
- it records with `project: bau`, `environment: prod`;
- the header comment says phase 3's Ansible play triggers it.

- [ ] **Step 1: Write the failing tests** (`tests/test_workflows.py`, parsing the YAML; note PyYAML reads the `on:` key as `True`):

```python
def test_release_publishes_only_from_main_and_in_order():
    jobs = load("ci.yml")["jobs"]
    assert jobs["images"]["needs"] == ["version"] or set(jobs["images"]["needs"]) == {"version"}
    assert {"version", "images", "web"} <= set(jobs["release"]["needs"])
    for job in ("images", "release"):
        publish_steps = [s for s in jobs[job]["steps"] if "push" in str(s.get("if", "")) or "gh release create" in s.get("run", "")]
        assert publish_steps and all("refs/heads/main" in str(s.get("if", "")) + str(jobs[job].get("if", "")) + str(jobs[job].get("env", "")) for s in publish_steps)

def test_images_are_arm64_only_and_never_latest():
    images = load("ci.yml")["jobs"]["images"]
    assert images["runs-on"] == "ubuntu-24.04-arm"
    text = yaml.safe_dump(images)
    assert "linux/arm64" in text and "linux/amd64" not in text and ":latest" not in text
    assert "--provenance=false" in text and "--sbom=false" in text

def test_bundle_smoke_runs_before_publishing():
    steps = load("ci.yml")["jobs"]["release"]["steps"]
    names = [s.get("name", "") for s in steps]
    assert names.index("bundle-smoke") < next(i for i, s in enumerate(steps) if "gh release create" in s.get("run", ""))

def test_secrets_only_in_the_release_environment():
    jobs = load("ci.yml")["jobs"]
    for name, job in jobs.items():
        if "secrets.NEXUS" in yaml.safe_dump(job):
            assert "release" in str(job.get("environment", "")), name

def test_ci_status_waits_for_the_release_jobs():
    assert {"version", "images", "release"} <= set(load("ci.yml")["jobs"]["ci-status"]["needs"])

def test_bau_installed_records_prod_for_a_real_release():
    wf = load("bau-installed.yml")
    text = yaml.safe_dump(wf)
    assert set(wf[True]["workflow_dispatch"]["inputs"]) == {"version", "host"}
    assert "bau-v" in text and "environment: prod" in text and "project: bau" in text
```

(The first test's `if` check is fiddly by nature. Implement the intent: every step that pushes an image or creates a release is gated on a push to `main`, whether through its own `if`, the job's `if` or a `PUBLISH` env flag as in home-servers. Simplify the assertion to match the chosen mechanism, and keep it asserting something.)

- [ ] **Step 2: Run:** `uv run pytest tests/test_workflows.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the jobs and the workflow. Then lint them: `uvx --from actionlint-py actionlint .github/workflows/*.yml`, or `actionlint` if it is installed. Expected: no findings.
- [ ] **Step 4: Run:** `uv run pytest -q`. Expected: PASS. Push the branch and open the PR. The PR run must pass `version`, `images` (build only) and `release` (bundle and smoke only), and the run log must show no push and no `gh release create`.
- [ ] **Step 5: Commit:** `feat: CI release pipeline and bau-installed workflow (BAU-24)`

### Task 7: Docs and the spec

**Files:**
- Modify: `docs/superpowers/specs/2026-10-14-bau-design.md` (the section 7 edits of the packaging spec, except deleting `install.sh`, which phase 4 does), `docs/superpowers/specs/2026-10-19-bau-packaging-design.md` (status line: "accepted 21 Oct 2026; phases 1 and 2 implemented by `docs/superpowers/plans/2026-10-21-bau-packaging.md`"; section 10 marked "all recommendations accepted"; section 4.3 and section 2.2's "four job definitions" corrected to three), `CLAUDE.md` (layout table: `config/` holds `host.example.yaml`; `scripts/`; policy lives in `bau/defaults.yaml`), `README.md` and `docs/runbook.md` (install steps: a pointer to phase 3's Ansible play, and the development loop `uv run bau --config config/` with a `config/host.yaml` copied from the example)
- Test: none (docs). The verification is a grep.

- [ ] **Step 1: Edit** the files above.
- [ ] **Step 2: Verify:** `git grep -n "projects.yaml\|bau/src\|config/bau.yaml\|/Users/nakomis/bau" -- ':!docs/superpowers/plans' ':!docs/superpowers/specs/2026-10-19-bau-packaging-design.md' ':!install.sh'`. Expected: only historical mentions (the original spec's dated amendment notes), each reading as history, not as instructions.
- [ ] **Step 3: Commit:** `docs: packaging in the spec, runbook and README (BAU-24)`

---

## After the PR merges (controller)

1. Watch the first `main` run publish `bau-v0.1.x`, both images and the `sandbox` record, and check the tracker shows it.
2. Set BAU's own ATC row with the plane MCP's `set_project_meta`: `deploys: true` and `deploy_action: "Bump bau_version in home-servers cere/ansible and run scripts/cere-ansible.sh --tags bau"`.
3. Write the phase 3 plan (home-servers: the install, migration and rollback tasks, `bau_version` pin, `certs.d` for `registry.home.nakomis.com`, removing P1's `bau_ref` pin). Its first run on Cere goes through Martin or the mace session.
