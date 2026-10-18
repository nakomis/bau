# BAU Questions Queue and Nightly Labelling Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every night, label every unlabelled, not-moving Plane item `BAU` or `not-bau`, triage every `BAU` item into ready / questions / too big, let Martin answer questions on the dashboard, and nudge him on BD-1 when the questions or PR queues grow.

**Architecture:** Two new night passes run inside `Night.run()` between feedback and new tickets: `bau/sweep.py` (rewritten: labels from Plane data only) and `bau/triage.py` (new: one read-only session per item, writes a verdict marker, questions or `not-bau`). All machine state lives in Plane comments written by `bau@nakomis.com`, parsed by a new pure module `bau/markers.py`. The API gains answers and not-bau routes (`bau/answers.py`), the snapshot gains a `questions` list and a `nudge` object, the dashboard gains a Questions section, and BD-1 (Rust, in home-infra) shows the new line.

**Tech Stack:** Python 3.12 / uv / pytest / ruff (host); FastAPI; Vite + React + Tailwind 4 + Biome + Vitest (web); Rust (BD-1 in `nakomis/home-infra`, `mac/conversation-memory-forwarder`).

**Spec:** `docs/superpowers/specs/2026-10-18-bau-questions-design.md`, amending `docs/superpowers/specs/2026-10-14-bau-design.md`. Plane: BAU-21. Branch: `bau-21-questions-queue`.

## Global Constraints

- Every Plane and GitHub write goes through `bau/plane.py` / `bau/github.py`, driven from `bau/runloop.py`, `bau/abandon.py`, `bau/sweep.py`, `bau/triage.py` or `bau/answers.py`. Update the CLAUDE.md line that lists these.
- Policy constants live in `config/bau.yaml`; tests build explicit `Limits(...)` / `Nudge(...)`.
- Labels, exactly: `BAU`, `bau-questions`, `not-bau` (plus existing `BAU-fable`, `blocked`).
- Markers, exactly (only honoured on comments authored by `bau@nakomis.com`):
  - `[bau:triage verdict=<ready|questions|too_big> round=<n> desc=<12 hex>]`
  - `[bau:questions round=<n> data=<base64url JSON>]`
  - `[bau:answers round=<n> data=<base64url JSON>]`
  - `[bau:swept verdict=<bau|not-bau>]`
- `max_question_rounds: 4`. Triage brief round line: `This is round {n} of at most {max}.`; when `n == max` add `This is the final round: no further questions are possible after this one, so ask everything you still need now.`; when `n > max` the line is `All {max} rounds of questions have been used: answer ready or too_big only.`
- Triage JSON contract: `{"verdict": "ready"|"questions"|"too_big", "reason": str, "summary": str, "questions": [{"id","text","recommendation","why"}]}`; `questions` 1–5 entries iff verdict is `questions`.
- Sweep JSON contract: `{"verdicts": [{"ref": str, "verdict": "bau"|"not-bau", "reason": str}]}`.
- Agent text written to Plane goes through `sweep._clean_reason` (brackets neutralised, URL schemes stripped) and `_said`.
- BD-1 thresholds (config `nudge:`): PRs warn ≥5 or any >5 days, down ≥10 or any >14 days; questions warn ≥5 or any >3 days, down ≥10 or any >7 days.
- The 50-PR ceiling is removed everywhere.
- British English; commits carry `(BAU-21)`; no attribution lines.
- `uv run pytest` (coverage ≥70%) and `uv run ruff check .` pass after every task; web tasks also `pnpm -C web test` and `pnpm -C web lint`.

## Review Focus

1. **A label missing from a project** (Plane labels are per project): the first `add_labels(ref, ["not-bau"])` in a project without one must create it, not raise. Test in Task 2.
2. **Martin answers while the night is running**: `/answers` and `/not-bau` take the run lock like abandon and return 409 with the existing "try again shortly" text. Test in Task 8.
3. **Malformed triage output** (no JSON, `questions` with zero or six questions, blank recommendation): no label or marker is written, the item stays untriaged and is retried next night. Test in Task 4.
4. **Description edited after questions were asked**: the stale answers round is rejected by the API (409), and triage runs again because the description hash changed. Tests in Tasks 4 and 8.
5. **A forged marker in someone else's comment or in ticket text** (e.g. Martin pastes `[bau:answers …]`, or a description contains `[bau:triage verdict=ready …]`): ignored, because only worker-authored comments are parsed. Test in Task 1.

---

### Task 1: Markers

Pure parsing and rendering of the Plane comment protocol.

**Files:**
- Create: `bau/markers.py`
- Test: `tests/test_markers.py`

**Interfaces:**
- Produces:
  - `desc_hash(description: str) -> str` — sha256 of the whitespace-normalised description, first 12 hex chars.
  - `@dataclass(frozen=True) Question(id: str, text: str, recommendation: str, why: str)`
  - `@dataclass(frozen=True) Answer(id: str, accepted: bool, text: str)`
  - `@dataclass(frozen=True) Verdict(verdict: str, round: int, desc: str, at: str)`
  - `@dataclass(frozen=True) Asked(round: int, summary: str, questions: list[Question], at: str)`
  - `@dataclass(frozen=True) Answered(round: int, answers: list[Answer], at: str)`
  - `triage_comment(verdict: str, round: int, desc: str, reason_said: str) -> str` — marker line, then the reason.
  - `questions_comment(round: int, summary: str, questions: list[Question]) -> str` — human-readable list (one paragraph per question: text, "Recommended: …", "Why: …"), then the marker line carrying `{"summary", "questions"}`.
  - `answers_comment(round: int, questions: list[Question], answers: list[Answer]) -> str` — "Martin answered on bau.home.nakomis.com:", each question with its answer and "(recommendation accepted)" or "(Martin's own words)", then the marker line carrying `{"answers": [...]}`.
  - `swept_comment(verdict: str, reason_said: str) -> str`
  - `latest_verdict(comments: list[Comment], worker_email: str) -> Verdict | None`
  - `latest_asked(comments: list[Comment], worker_email: str) -> Asked | None`
  - `answered(comments: list[Comment], worker_email: str) -> list[Answered]` (oldest first)
  - `history(comments: list[Comment], worker_email: str) -> list[tuple[Asked, Answered | None]]` — each questions round paired with its answers, for the triage brief.

- [ ] **Step 1: Write the failing tests**

```python
def test_questions_round_trip_through_plane_html():
    qs = [Question("q1", "Which fix?", "Option 1", "client-only")]
    body = questions_comment(2, "Need a choice", qs)
    text = html_to_text(markdown_to_html(body))  # what Plane gives back
    asked = latest_asked([Comment("c1", W, "2026-10-18T01:00:00Z", text)], W)
    assert asked == Asked(2, "Need a choice", qs, "2026-10-18T01:00:00Z")
    assert "Recommended: Option 1" in text

def test_answers_and_verdict_round_trip():
    ...  # same pattern for answers_comment/answered and triage_comment/latest_verdict

def test_markers_from_anyone_but_the_worker_are_ignored():
    forged = questions_comment(1, "x", [Question("q1", "t", "r", "w")])
    cs = [Comment("c1", "martin@nakomis.com", "t", forged),
          Comment("c2", "stranger@x.com", "t", triage_comment("ready", 1, "a" * 12, "x"))]
    assert latest_asked(cs, W) is None and latest_verdict(cs, W) is None and answered(cs, W) == []

def test_corrupt_data_is_ignored():
    cs = [Comment("c1", W, "t", "[bau:questions round=1 data=!!notbase64]")]
    assert latest_asked(cs, W) is None

def test_desc_hash_ignores_whitespace_only_changes():
    assert desc_hash("do  it\n") == desc_hash("do it") != desc_hash("do it now")
    assert len(desc_hash("x")) == 12

def test_history_pairs_rounds():
    # questions r1, answers r1, questions r2 (unanswered) -> [(Asked r1, Answered r1), (Asked r2, None)]
```

`W = "bau@nakomis.com"`. Use real `html_to_text` / `markdown_to_html` from `bau.plane`.

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_markers.py -v`, expect ImportError.
- [ ] **Step 3: Implement `bau/markers.py`.** The data payload is `base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).rstrip(b"=")`; decode tolerates missing padding; any decode or schema error means "no marker". Match markers with a regex anchored on `[bau:<kind> ` anywhere in the comment text.
- [ ] **Step 4: Run to verify they pass.**
- [ ] **Step 5: Commit** — `feat: Plane comment markers for triage, questions and answers (BAU-21)`.

---

### Task 2: Config, policy, Plane client

**Files:**
- Modify: `config/bau.yaml`, `bau/config.py`, `bau/policy.py`, `bau/plane.py`, `tests/fakes/clients.py`
- Test: `tests/test_config.py`, `tests/test_policy.py`, `tests/test_plane.py`

**Interfaces:**
- Produces:
  - `Limits`: remove `open_pr_ceiling`; add `triage_budget_usd: float` (1.25), `triage_wall_clock_minutes: int` (10), `max_question_rounds: int` (4), `sweep_owner_quiet_days: int` (7), `sweep_batch_items: int` (40). Keep `sweep_session_wall_clock_minutes` (15).
  - `Schedule`: remove `sweep_no_new_sessions_after`.
  - `@dataclass(frozen=True) Nudge(prs_warn_count: int, prs_warn_days: int, prs_down_count: int, prs_down_days: int, questions_warn_count: int, questions_warn_days: int, questions_down_count: int, questions_down_days: int)` with the values from Global Constraints; `Config.nudge: Nudge` loaded from a top-level `nudge:` block.
  - `ProjectConfig.repo: str | None`; `load_projects` accepts `repo: null`.
  - `Policy.can_start_session(*, latest, sessions_started, paused) -> Decision` — `open_prs` and `new_ticket` parameters and the `ceiling` reason are removed.
  - `Item` gains `state_group: str = ""` and `updated_by: str = ""` (member email), filled by `PlaneClient._format`.
  - `PlaneClient.add_labels(ref, names)` creates any label missing from the project (`POST /projects/{id}/labels/` with `{"name": n}`), updates the cached project meta, then patches.
  - `FakePlane` gains `add_labels` / `remove_labels` that record calls and update the item's labels, and `list_items` keeps working with the new fields.

- [ ] **Step 1: Write failing tests:** `test_load_config_reads_nudge_and_new_limits` (asserts `cfg.nudge.questions_warn_days == 3`, `cfg.limits.max_question_rounds == 4`, no `open_pr_ceiling` attribute); `test_load_projects_allows_null_repo`; `test_policy_has_no_ceiling` (`can_start_session(latest=good, sessions_started=0, paused=False).allowed` with any number of PRs); `test_add_labels_creates_missing_label` (mock transport: GET labels lacks `not-bau` → one POST to `/labels/` then PATCH containing the new id); `test_format_fills_state_group_and_updated_by`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Update every `can_start_session` caller (`Night.may_start` loses `new_ticket`; `Sweep` is rewritten in Task 3) and every test that built `Limits(open_pr_ceiling=…)`.
- [ ] **Step 4: Run the whole suite** — `uv run pytest`, all green.
- [ ] **Step 5: Commit** — `feat: config for triage, rounds and nudges; drop the PR ceiling (BAU-21)`.

---

### Task 3: The labelling sweep

Rewrite `bau/sweep.py` as a night pass. It reads only Plane; it labels.

**Files:**
- Modify: `bau/sweep.py` (rewrite), `bau/runloop.py` (session helper only)
- Test: `tests/test_sweep.py` (rewrite)

**Interfaces:**
- Consumes: `markers.swept_comment`, `Limits.sweep_*`, `Item.state_group/updated_by`, `REPO_LINE` from `runloop`.
- Produces:
  - `SWEEP_LABELS = ("BAU", "bau-questions", "not-bau")`
  - `sweepable(item: Item, comments: list[Comment], *, owner_email: str, worker_email: str, quiet_days: int, now: datetime) -> bool` — no label from `SWEEP_LABELS` or `blocked`; `state_group in ("backlog", "unstarted")`; assignees ⊆ `{worker_email}`; no owner comment and no owner edit (`updated_by == owner_email`) within `quiet_days`.
  - `@dataclass(frozen=True) SweepVerdict(ref: str, verdict: str, reason: str)`
  - `build_sweep_brief(identifier: str, items: list[Item], nonce: str | None = None) -> tuple[str, set[str]]` — prompt asks for a `bau` / `not-bau` verdict per item: `bau` = small, self-contained, could be done from the ticket alone or after a quick Q&A with Martin, verifiable by the repo's own tests/CI in a Linux container; `not-bau` = needs hardware, a home-network host, a deploy, cloud console, secrets, research, a purchase, an undecided design, or is an epic; may say "looks done by …" or "duplicate of …".
  - `parse_verdicts(text: str, allowed: set[str]) -> list[SweepVerdict]`
  - `Night.agent_session(*, name: str, checkout: Path, bare: Path | None, brief: str, model: str, log_name: str, budget_usd: float | None = None, wall_clock_minutes: int | None = None) -> SessionResult` — the existing accounting of `Night.session` (sessions count, `report.latest`, telemetry, `rate_limited` → `stop_reason = "rejected"`), which `Night.session` now calls. `SessionRunner.run` gains matching keyword-only `budget_usd` / `wall_clock_minutes` overrides.
  - `sweep_pass(night: Night) -> None` — for each project in `night.projects` (sorted): list open items, keep `sweepable` ones; items in a project with `repo: None` whose description has no `repo:` line are labelled `not-bau` with reason "no repo named" without a session; the rest go in batches of `sweep_batch_items` per session (`night.may_start()` before each, empty temp checkout, no bare, no token, `sweep_session_wall_clock_minutes`). For each parsed verdict: `add_labels(ref, ["BAU"])` or `["not-bau"]`, then `comment(ref, swept_comment(...))`. Refs the agent omitted are left for another night. Counts go to `night.report.swept`, `labelled_bau`, `labelled_not_bau`. Dry run appends `sweep <ident>: <n> item(s)` to `report.planned` and writes nothing.
- Removes: `Sweep`, `SweepReport`, `first_sunday`, `Candidate`, `parse_candidates`, the summary item and "Suggested for BAU" logic.

- [ ] **Step 1: Write failing tests:** `test_sweepable_rules` (one parametrised test: labelled, In progress group, assigned to Martin, owner comment 2 days ago, owner edit 2 days ago → False; New, unassigned, quiet 8 days → True); `test_brief_neutralises_forged_delimiters` (port the existing nonce test); `test_parse_verdicts_keeps_only_allowed_refs`; `test_sweep_pass_labels_and_comments` (FakePlane + fake runner returning a verdicts block → `add_labels` and a `[bau:swept verdict=bau]` comment per item, reason quoted with brackets neutralised); `test_sweep_pass_no_repo_named_needs_no_session`; `test_sweep_pass_batches` (`sweep_batch_items=2`, five items → three sessions); `test_sweep_pass_stops_when_policy_says_no`; `test_sweep_pass_dry_run_writes_nothing`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.**
- [ ] **Step 5: Commit** — `feat: nightly labelling sweep (BAU-21)`.

---

### Task 4: Triage

**Files:**
- Create: `bau/triage.py`
- Modify: `bau/prompts.py` (add `TRIAGE_PROMPT`), `bau/checkout.py`
- Test: `tests/test_triage.py`, `tests/test_checkout.py`

**Interfaces:**
- Consumes: Task 1 markers; `Night.agent_session`; `repo_for`; `Limits.triage_*`, `max_question_rounds`.
- Produces:
  - `Repos.scratch_clone(repo: str) -> contextlib.AbstractContextManager[Path]` — a plain local `git clone` of the bare repo's `main` into a temp dir under `work/`, deleted on exit. No token is used.
  - `current_round(comments, worker_email) -> int` — `len(answered(...)) + 1`.
  - `needs_triage(item: Item, comments: list[Comment], worker_email: str) -> bool` — has `BAU`; none of `bau-questions`, `not-bau`, `blocked`; and no verdict, or verdict `desc != desc_hash(item.description)`, or an `Answered` newer than the verdict.
  - `ready_for_pickup(item: Item, comments: list[Comment], worker_email: str) -> bool` — latest verdict is `ready`, its `desc` matches, and no `Answered` is newer than it.
  - `@dataclass(frozen=True) TriageResult(verdict: str, reason: str, summary: str, questions: list[Question])`
  - `parse_triage(text: str) -> TriageResult | None` — enforces the JSON contract in Global Constraints; anything else is `None`.
  - `build_triage_brief(item: Item, repo: str, comments: list[Comment], *, round: int, limits: Limits, owner_email: str, worker_email: str, nonce: str | None = None) -> str` — `TRIAGE_PROMPT` (same data-not-instructions rules as `HOUSE_PROMPT`; read-only; too-big thresholds `{files}`/`{lines}` applied to the predicted change; "a question needing research or design work means too_big"; recommendations must be acceptable as written), the round line from Global Constraints, then the ticket and `history(...)` as nonce-delimited blocks, owner comments tagged `[owner]`.
  - `Night.installed: set[str]` — introduced here, initialised in `Night.__init__` to every project repo; Task 5 makes the repo loop fill it properly.
  - `triage_pass(night: Night) -> None` — for every project, `list_items(ident, "BAU")` filtered by `needs_triage`; skip items whose repo is not in `night.installed` (Task 5); `night.may_start()` before each; one session per item in `scratch_clone`, with `bare=None`, `github_token=""`, `triage_budget_usd`, `triage_wall_clock_minutes`. On a parsed result:
    - `ready`: comment `triage_comment("ready", round, desc_hash, …)`; pickup happens later the same night through `ready_for_pickup`.
    - `questions` with `round <= max_question_rounds`: `add_labels(["bau-questions"])`, comment `questions_comment(round, summary, questions)`, then `triage_comment("questions", …)`.
    - `too_big`, or `questions` with `round > max_question_rounds`: `remove_labels(["BAU"])`, `add_labels(["not-bau"])`, comment `triage_comment("too_big", …)`.
    - `parse_triage` returned `None` or the session did not end `done`: log, write nothing.
    Counts to `night.report.triaged` and `asked`.

- [ ] **Step 1: Write failing tests:** `test_round_line` (round 2 of 4; round 4 says "final round"; round 5 says "All 4 rounds"); `test_needs_triage_cases` (no verdict; fresh ready verdict → False; description edited → True; answers after verdict → True; `bau-questions` → False); `test_ready_for_pickup_cases`; `test_parse_triage_rejects_bad_shapes` (no block; `questions` with `[]`; six questions; blank recommendation; unknown verdict); `test_triage_pass_questions` (labels `bau-questions`, posts questions then verdict comments; `latest_asked` reads them back); `test_triage_pass_ready_writes_verdict`; `test_triage_pass_too_big_marks_not_bau`; `test_questions_after_four_rounds_becomes_too_big`; `test_bad_output_writes_nothing`; `test_scratch_clone_is_removed` (in `test_checkout.py`, real git on a temp bare repo).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.**
- [ ] **Step 5: Commit** — `feat: nightly triage with questions and recommended answers (BAU-21)`.

---

### Task 5: Night integration

**Files:**
- Modify: `bau/runloop.py`, `bau/abandon.py`
- Test: `tests/test_runloop.py`, `tests/test_abandon.py`

**Interfaces:**
- Consumes: `sweep_pass`, `triage_pass`, `ready_for_pickup`.
- Produces:
  - `Night.installed` (from Task 4) now holds only repos whose installation token minted during the repo loop in `run()`; a failure there is logged and appended to `NightReport.uninstalled: list[str]` instead of ending the night. Merge, ready, feedback and new-ticket work skip repos not in `installed`; projects with `repo: None` contribute no repo.
  - `NightReport` gains `swept`, `labelled_bau`, `labelled_not_bau`, `triaged`, `asked: int = 0` and `uninstalled: list[str]`.
  - `run()` order: preflight, probe, repo loop, `merge_pass`, `ready_pass`, `may_start` check, `feedback_pass`, `sweep_pass(self)`, `triage_pass(self)`, `new_tickets_pass()`. Preflight uses the first project that has a repo.
  - `new_tickets_pass` skips items labelled `bau-questions` or `not-bau`, and items failing `ready_for_pickup` (comments fetched once per item and reused by `failed_attempts`).
  - Too big (session `too_big` in `work_picked_up`, and `close_too_big`): additionally `remove_labels(["BAU"])` and `add_labels(["not-bau"])`; comment text ends "Marked not-bau."
  - `abandon`: after removing `BAU`, `add_labels(ref, ["not-bau"])`; comment `[bau:abandoned] by Martin. Returned to {restored}; marked not-bau.`

- [ ] **Step 1: Write failing tests:** `test_run_order_sweep_then_triage_then_pickup` (record pass order with monkeypatched passes); `test_untriaged_bau_item_is_not_picked_up`; `test_triaged_ready_item_is_picked_up_same_night`; `test_uninstalled_repo_does_not_end_the_night` (`gh_app.installation_token` raises for one repo; the other repo's work proceeds; `report.uninstalled == [repo]`); `test_too_big_marks_not_bau`; `test_abandon_marks_not_bau`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Adjust existing run-loop tests that relied on the ceiling or on untriaged pickup by giving their items a fresh `ready` verdict comment.
- [ ] **Step 4: Run the suite.**
- [ ] **Step 5: Commit** — `feat: sweep and triage in the nightly run; too big and abandon mark not-bau (BAU-21)`.

---

### Task 6: Snapshot and nudge

**Files:**
- Modify: `bau/snapshot.py`, `bau/cli.py` (`snapshot_rows`, `_write_run_snapshot`, the `snapshot` command)
- Test: `tests/test_snapshot.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) QuestionRow(repo: str, ref: str, title: str, plane_url: str, repo_url: str, asked_at: str, age_days: int, round: int, summary: str, questions: list[dict])`
  - `LastRun` gains `swept`, `labelled_bau`, `labelled_not_bau`, `triaged`, `asked` (default 0, so old snapshots still parse in `_last_run`).
  - `nudge(open_prs: list[OpenPrRow], questions: list[QuestionRow], t: Nudge) -> dict` → `{"level": l, "prs": l1, "questions": l2}` where each is `"ok" | "warn" | "down"` and `level` is the worst.
  - `build_snapshot(..., questions: list[QuestionRow], nudge_thresholds: Nudge, uninstalled: list[str])` writes `"schema": 2`, `"questions"`, `"nudge"`, `"uninstalled"`.
  - `drop_question(path: Path, ref: str) -> None` — mirrors `drop_open_pr`.
  - `snapshot_rows` returns `(open_rows, deploy_rows, question_rows)`; question rows come from `plane.list_items(ident, "bau-questions")` plus `latest_asked(comments, worker)`; an item with the label but no parsable questions is skipped with a log line. `repo_url` is `https://github.com/<repo>`.

- [ ] **Step 1: Write failing tests:** `test_nudge_levels` (parametrised over the thresholds: 4 PRs all fresh → ok; 5 → warn; one PR 6 days old → warn; 10 → down; one question 4 days → warn; 8 days → down; level is the worst); `test_snapshot_schema_2_has_questions_and_nudge`; `test_drop_question`; `test_snapshot_rows_builds_question_rows` (FakePlane item labelled `bau-questions` with a worker questions comment).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.**
- [ ] **Step 5: Commit** — `feat: questions and nudge in the snapshot (BAU-21)`.

---

### Task 7: CLI, launchd and docs clean-up

**Files:**
- Modify: `bau/cli.py`, `install.sh`, `README.md`, `CLAUDE.md`
- Delete: `launchd/com.nakomis.bau-sweep.plist`
- Test: `tests/test_cli.py`, `tests/test_container_files.py` (if it lists plists)

**Interfaces:**
- Removes the `sweep` subcommand, `sweep_due`, `SWEEP_LOCK_WAIT`, `today`.
- `install.sh`: drops `bau-sweep` from its label loop and, if `~/Library/LaunchAgents/com.nakomis.bau-sweep.plist` exists, `launchctl bootout`s it and deletes it.
- `bau run --dry-run` prints sweep, triage and new-ticket plans (already collected in `report.planned`).
- README launchd list and CLAUDE.md writer list updated (`bau/triage.py`, `bau/answers.py` added; monthly sweep gone).

- [ ] **Step 1: Write failing tests:** `test_sweep_subcommand_is_gone` (`build_parser().parse_args(["sweep"])` raises `SystemExit`); `test_no_sweep_plist`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite and `bash -n install.sh`.**
- [ ] **Step 5: Commit** — `chore: remove the monthly sweep job (BAU-21)`.

---

### Task 8: Answers and not-bau API

**Files:**
- Create: `bau/answers.py`
- Modify: `bau/api.py`
- Test: `tests/test_answers.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: Task 1 markers, `abandon.REF_PATTERN`, `abandon.InvalidRef/UnknownProject/RunInProgress/RUN_IN_PROGRESS`, `abandon.clean_note`, `lock.run_lock`, `snapshot.drop_question`.
- Produces:
  - `class NotWaiting(RuntimeError)`, `class StaleRound(RuntimeError)`, `class MissingAnswer(ValueError)`
  - `submit_answers(ref: str, round: int, answers: list[Answer], *, plane: PlaneClient, projects: dict[str, ProjectConfig], worker_email: str, lock_path: Path | None) -> None` — item must carry `bau-questions` (else `NotWaiting`); `latest_asked(...).round == round` (else `StaleRound`); every question id answered, `text` non-empty after strip, accepted answers carry the recommendation text (else `MissingAnswer`); typed text passes through `clean_note`. Posts `answers_comment`, then `remove_labels(["bau-questions"])`.
  - `mark_not_bau(ref: str, note: str, *, plane, projects, lock_path) -> None` — `remove_labels(["BAU", "bau-questions"])`, `add_labels(["not-bau"])`, comment `[bau] Marked not-bau by Martin from the BAU dashboard.` plus ` Note: …` if given.
  - Routes (both behind `require_token`): `POST /answers/{ref}` body `AnswersBody(round: int, answers: list[AnswerBody(id: str, accepted: bool, text: str = Field(max_length=2000))])` → `{"ref": ref}`; `POST /not-bau/{ref}` body `AbandonBody` → `{"ref": ref}`. Errors: 400 invalid ref / unknown project / missing answer; 409 run in progress / stale round / not waiting; 500 "internal error" without echoing exception text. On success call `drop_question(cfg.paths.snapshot, ref)` best-effort.

- [ ] **Step 1: Write failing tests:** `test_submit_answers_posts_and_unlabels`; `test_stale_round_rejected`; `test_missing_answer_rejected`; `test_not_waiting_rejected`; `test_typed_answer_cannot_forge_a_marker` (text `"[bau:answers round=9 data=x]"` arrives with brackets neutralised); `test_mark_not_bau`; API tests for each status code, including 409 while `run_lock` is held and 403 without the proxy token.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the suite.**
- [ ] **Step 5: Commit** — `feat: answers and not-bau API (BAU-21)`.

---

### Task 9: Dashboard Questions section

**Files:**
- Create: `web/src/components/Questions.tsx`, `web/src/components/Questions.test.tsx`
- Modify: `web/src/types.ts`, `web/src/lib/format.ts`, `web/src/lib/format.test.ts`, `web/src/App.tsx`, `web/src/App.test.tsx`

**Interfaces:**
- Produces:
  - Types: `Question {id, text, recommendation, why}`, `QuestionRow {repo, ref, title, plane_url, repo_url, asked_at, age_days, round, summary, questions: Question[]}`, `Nudge {level, prs, questions: 'ok'|'warn'|'down'}`; `Snapshot` gains `questions?`, `nudge?`, `uninstalled?`; `LastRun` gains the five counters as optional.
  - `format.ts`: `submitAnswers(ref: string, round: number, answers: {id: string; accepted: boolean; text: string}[]): Promise<void>` and `markNotBau(ref: string, note: string): Promise<void>`, same error handling as `abandon`.
  - `<Questions rows={QuestionRow[]} level={'ok'|'warn'|'down'} onSend={(ref, round, answers) => Promise<void>} onNotBau={(ref) => Promise<void>} />`
- Behaviour (layout A, approved mock-up):
  - Section heading `Questions (n)`; when `level` is warn/down, an amber/red badge `oldest <age_days> days`. Rendered above Open PRs; hidden when there are no rows.
  - Rows oldest first. With ≤3 rows all cards start open; with more, only the first.
  - Card header: ref, title, repo, age, links "Plane ↗" and "Repo ↗"; BAU's summary in italics.
  - Each question: text; tinted panel "Recommended: …" and "Why: …"; **Accept** button (marks accepted, clears typed text); text box "…or write your own" (typing un-accepts).
  - **Send to BAU** disabled until every question is accepted or has non-blank text. On success: card removed, next card opens, one-line notice `"<ref>: sent to BAU"` via the existing notices list. On failure: the error text under the button; card stays.
  - **Not BAU**: calls `onNotBau`, same remove-and-open-next behaviour, notice `"<ref>: marked not-bau"`.
  - Drafts (per ref and round) saved to `localStorage` key `bau.drafts`, every access in try/catch; restored on load; cleared on send.
  - `uninstalled` repos (if any) listed in one muted line under the section: "Waiting for the GitHub App: repo, repo".

- [ ] **Step 1: Write failing tests** (Vitest + Testing Library): `renders recommendation and why`; `send disabled until every question answered`; `accept then send posts accepted answers`; `typed answer overrides recommendation`; `send removes the card and opens the next`; `not BAU removes the card and opens the next`; `more than three rows start with only the first open`; `drafts survive a remount and clear on send`; `localStorage throwing does not break the page`; App test: questions section appears above Open PRs and is hidden when empty.
- [ ] **Step 2: Run to verify they fail** — `pnpm -C web test`.
- [ ] **Step 3: Implement**, matching the existing Tailwind idiom in `PrTable.tsx` and `App.tsx`.
- [ ] **Step 4: Run** `pnpm -C web test && pnpm -C web lint && pnpm -C web build`.
- [ ] **Step 5: Commit** — `feat: questions section on the dashboard (BAU-21)`.

---

### Task 10: Projects for every code repo

**Files:**
- Modify: `config/projects.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ProjectConfig.repo: str | None` (Task 2).
- Starting mapping (from the 18 Oct sweep; verify each with `gh repo view nakomis/<repo> --json name,isPrivate,defaultBranchRef` and fix any that does not exist): ADMIN nakom-admin, BAPP blog-app, BOOT bootboots, CHATS claude-chats, CLOUD nakomis-infra, CRYPT tinycrypt, CSSC cdk-ssm-secret-checker, CTHU cthulhu, EYE mcps, FLEET nakomis-fleet, HAND handmade-intelligence, KAPPA kappa-warmer, KBRD mechanical-keyboard, LAPC lapcat, LIGHT lightning, MUSH mushroom-humidor, NAKIS nakom.is, NAKTV naktv, NDFM nakomis-dragonfruit-mcp, PIOE epio, PIPE blog-pipeline, PISH pish, PLAY playlist, RECIP recipator, SCHIM schimmelpfennig, SCOPE nakoscope, SCRUM nakomis-scrum, SINT sinter, STAT nakostat, TVRM tv-remote; PROJ and MULTI with `repo: null`. Leave out BCON, HARM, MONEY, BENCH.
- `deploys: true` only where the repo has a deploy workflow (`gh api repos/nakomis/<repo>/contents/.github/workflows` lists a file whose name contains `deploy`) or is home-infra; `deploy_action` then says "Approve the prod deploy in GitHub Actions" unless the repo's README says otherwise. Record anything uncertain in the PR body under "What Martin needs to decide".

- [ ] **Step 1: Write the failing test** `test_projects_yaml_covers_every_code_project` — loads the real file; asserts BAU, HOME and the 30 identifiers above are present, PROJ and MULTI have `repo is None`, and none of BCON/HARM/MONEY/BENCH appear.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Fill in `projects.yaml`** after the `gh` checks.
- [ ] **Step 4: Run the suite.**
- [ ] **Step 5: Commit** — `feat: list every code project for BAU (BAU-21)`.

---

### Task 11: BD-1 line (home-infra)

A separate PR in `nakomis/home-infra`, branch `bau-21-bd1-questions`, title `feat(bd1): BAU questions and nudge levels (BAU-21)`.

**Files:**
- Modify: `mac/conversation-memory-forwarder/src/status.rs` (`BauStatus`, `bau_status_from`, `BauStatus::health`), `mac/conversation-memory-forwarder/src/tray.rs` (`bau_line` and its tests)

**Interfaces:**
- Consumes: snapshot schema 2 fields `questions` (array) and `nudge.level` (`"ok"|"warn"|"down"`).
- Produces: `BauStatus.questions: Option<u32>` and `BauStatus.nudge: Option<Health>`; line text `BAU: {p} PRs · {q} questions · {run}{stale}`; health is the worst of the existing run-health rules (failed run, stale snapshot, missing telemetry) and `nudge`; when `nudge` is absent (schema 1), the existing PR-count thresholds apply unchanged.

- [ ] **Step 1: Write failing tests** in `tray.rs`: `bau_line_shows_questions` (`"BAU: 2 PRs · 3 questions · last run ok (cutoff)"`); `nudge_down_is_red_even_with_a_good_run`; `schema_1_snapshot_keeps_old_thresholds`.
- [ ] **Step 2: Run** `cargo test -p conversation-memory-forwarder` (or the crate's documented test command) to see them fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and `cargo clippy`.**
- [ ] **Step 5: Commit and open the home-infra PR**, linking the bau PR; note it can merge independently because schema 1 keeps working.

---

### Task 12: Close-out

- [ ] **Step 1:** In the 14 Oct spec's header, add `Amended by 2026-10-18-bau-questions-design.md (questions queue, nightly labelling sweep).`
- [ ] **Step 2:** Run `uv run pytest`, `uv run ruff check .`, `pnpm -C web test`, `pnpm -C web lint`; all green.
- [ ] **Step 3:** Commit `docs: point the original spec at the questions amendment (BAU-21)`, push, open the bau PR `feat: questions queue and nightly labelling sweep (BAU-21)` whose body lists Martin's prerequisites (spec section 10) and the repos from Task 10 needing the App checkbox and branch protection.
- [ ] **Step 4:** Attach both PR links to BAU-21 and comment that BAU-11 (per-project ceilings) is moot; leave closing it to Martin.
