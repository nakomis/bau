# bau

Overnight BAU maintenance worker: Plane items in, pull requests out. Spec:
`docs/superpowers/specs/2026-10-14-bau-design.md`. Read it before changing policy.

## Conventions

- Python 3.12 managed with `uv`: `uv sync`, `uv run pytest`, `uv run ruff check .`. Coverage floor 70%.
- Host-side code (`bau/`) runs on Cere as `nakomis`; the agent runs inside `container/`. The agent never talks to Plane.
- Every write to Plane or GitHub happens through `bau/plane.py` and `bau/github.py`, driven from `bau/runloop.py`, `bau/abandon.py`, `bau/sweep.py`, `bau/triage.py` or `bau/answers.py`; nothing else writes.
- Policy constants live in `bau/defaults.yaml` (packaged in the wheel), never in code. Per-host facts are the optional `$BAU_HOME/config/host.yaml` overlay, documented in `config/host.example.yaml`. Tests build explicit `Limits(...)`.
- The project list (repo, deploys, deploy action) and the order of work come from ATC (`bau/atc.py`: `GET /api/projects?bau_enabled=true`, `GET /api/priority`), cached at `paths.atc_cache`; change a project's BAU settings in ATC, not here. There is no project file. The per-ticket `repo:` override stays in BAU. Tests use a fake ATC (`httpx.MockTransport`, see `tests/test_atc.py`).
- Secrets come from the macOS login keychain, service `bau` (`bau/secrets.py`); never from files in the repo.
- Always work on a feature branch and open a PR. Branch and PR title carry the Plane ref, e.g. `bau-3-config` and `feat: config loading (BAU-3)`.
- British English throughout. No attribution lines in commits.

## Repository layout

| Path | Purpose |
|------|---------|
| `bau/` | Host-side Python |
| `container/` | Agent image, egress proxy compose |
| `config/` | `host.example.yaml`, the documented overlay for `$BAU_HOME/config/host.yaml` |
| `scripts/` | `build-bundle.sh`: builds the release bundle (wheel, requirements, plist templates, compose) |
| `launchd/` | Scheduled job templates (`*.plist.in`) |
| `web/` | Dashboard |
| `tests/` | pytest, with fakes under `tests/fakes/` |

## Testing

`uv run pytest` (70% minimum, enforced in `pyproject.toml`). Run `uv run ruff format --check .` too: ruff formats Python code blocks in Markdown. Session tests use a fake `docker` on `PATH`; see `tests/fakes/fake_docker.py`.

## Architecture diagrams

Source: `docs/architecture/bau.drawio` — SVG auto-regenerated on commit by `.githooks/pre-commit`.

To activate the hook after cloning:
```bash
git config core.hooksPath .githooks
```
