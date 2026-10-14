# bau — Overnight BAU maintenance worker: Plane items in, pull requests out

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=bau)

## Table of Contents

<!-- toc -->

- [What it is](#what-it-is)
- [How it runs](#how-it-runs)
- [Repository layout](#repository-layout)
- [Install on Cere](#install-on-cere)
- [Architecture Diagram](#architecture-diagram)
- [Architecture Diagrams](#architecture-diagrams)
- [Support](#support)

<!-- tocstop -->

## What it is

BAU ("business as usual") is a nightly worker that runs on Cere, picks up Plane work items
labelled `BAU`, works each one in a disposable Linux container, raises one pull request per
item, and never merges or deploys. Martin reviews the PRs when he gets round to it, from
`bau.home.nakomis.com`, and BD-1 shows how many are waiting.

The design is in `docs/superpowers/specs/2026-10-14-bau-design.md`; the implementation plans
are in `docs/superpowers/plans/`.

## How it runs

Three launchd jobs on Cere, all as the `nakomis` user:

- `com.nakomis.bau-run` at 01:00 every night: the worker itself.
- `com.nakomis.bau-snapshot` hourly during the day: refreshes `state.json` for the dashboard.
- `com.nakomis.bau-sweep` on the first Sunday of the month: proposes BAU candidates.

Usage is bounded by a pace rule read from Claude Code's own rate-limit telemetry, a 05:00
cut-off, and per-session budget and wall-clock limits, so the worker never eats the week.

## Repository layout

| Path | Purpose |
|------|---------|
| `bau/` | Host-side Python: policy, Plane and GitHub clients, the nightly loop, the CLI |
| `container/` | The agent image and the egress allow-list proxy |
| `config/` | `bau.yaml` (policy and paths) and `projects.yaml` (Plane project to repo map) |
| `launchd/` | The scheduled jobs |
| `web/` | The dashboard (Vite + React) |
| `terraform/` | GitHub App installation repositories |
| `docs/` | Spec, plans, runbook, architecture diagram |

## Install on Cere

See `docs/runbook.md`. In short: three keychain items, `git clone` to `~/bau/src`, `./install.sh`.

## Architecture Diagram

![Architecture](docs/architecture/bau.svg)

## Architecture Diagrams

`docs/architecture/bau.drawio` is the source for the diagram above.
The SVG is auto-regenerated on commit by the pre-commit hook in `.githooks/pre-commit`.

To activate the hook after cloning:

```bash
git config core.hooksPath .githooks
```

## Support

If you find this useful, please consider buying me a coffee:

[![Donate with PayPal](https://www.paypalobjects.com/en_GB/i/btn/btn_donate_SM.gif)](https://www.paypal.com/donate?hosted_button_id=Q3BESC73EWVNN&custom=bau)
