# BAU questions queue and labelling sweep: design

Date: 18 October 2026
Status: approved by Martin 18 Oct 2026 (session in `nakomis/bau`).
Amends: `2026-10-14-bau-design.md`. Where the two disagree, this document wins. Sections it replaces are named in section 9.

Amended during implementation (18 Oct 2026): the fix wave changed the following, and where the sections below disagree, this paragraph wins. Triage failures are now counted: a session that ends without a verdict, or malformed triage output, writes one `[bau:triage-failed]` comment (no label, no verdict) and the item is retried next night; after `max_triage_failures` (2, in `config/bau.yaml`) the item is declined in the run report and nothing more is written to Plane, until a comment from Martin or a verdict resets the count. Sweep batches are regrouped nightly by a date-seeded shuffle, so an item that sinks a session does not hold back the same neighbours every night. PROJ and MULTI items whose repo line names a repo absent from `projects.yaml` get `not-bau` with "repo not configured for BAU", without a session; session-free sweep writes also respect the pause and the cut-off. Ticket and feedback (and CI-fix) briefs now include the questions history, with Martin's answers as owner text. Triage skips items in finished states and items with an open BAU PR, and re-triages a plain BAU item unless it holds a current ready verdict. Marker positions are fixed: the triage and swept markers open a comment, and the questions and answers data markers are the whole last line, so agent text cannot forge them; agent-derived reasons are cleaned before posting.

## 1. Intent

BAU exists to spend Claude Max credit that would otherwise go unused on the easy end of Martin's backlog. Version one only works items Martin remembers to label `BAU`, and only those it can do from the ticket alone. A manual sweep on 18 Oct 2026 found about a dozen ready items in roughly 490 open ones, and about as many again that needed one or two decisions from Martin before they would be ready.

This change widens the funnel at both ends:

- **Nothing is missed because nobody labelled it.** A ticket raised by Martin or by a Claude session and never labelled is found by the sweep and labelled on its own.
- **Nothing stalls because one decision is missing.** If a ticket needs a quick answer, BAU asks, offers its own recommended answer, and waits. Martin answers at his desk, with the ticket and repo open, on `bau.home.nakomis.com`.

The pipeline becomes:

```
ticket raised
   ├─ Martin labels BAU ─────────────┐
   └─ nightly sweep labels BAU ──────┤        (or not-bau, never looked at again)
                                     ▼
                          nightly triage of BAU items
             ┌───────────────┬───────┴──────────┐
           ready         questions          too big
             │               │                  │
             │      bau-questions label,    not-bau + comment
             │      questions in the UI
             │               │
             │      Martin answers → BAU again (next night's triage)
             ▼
   pickup → PR → CI → review → merge → Ready for test / Done
```

Success looks like: Martin never labels a ticket by hand unless he wants to; the questions queue holds a handful of items he clears in one sitting; Plane always shows what BAU thinks of each item.

## 2. Goals, non-goals, anti-goals

**Goals**
- A nightly sweep that judges every unlabelled open item in an eligible project and labels it `BAU` or `not-bau`, unbounded in number.
- A nightly triage that decides, for each `BAU` item, ready / questions / too big, before any PR work starts on it.
- A questions queue on the existing dashboard, with recommended answers Martin can accept in one click.
- BD-1 nudges when either the questions queue or the PR queue gets too big or too old.

**Non-goals**
- Closing, deduplicating or changing the state of items. The sweep labels and comments; it never closes. (It may say in its `not-bau` comment that an item looks done or duplicated, which is useful to Martin but changes nothing.)
- Phone-friendly answering. Martin answers at his desk.
- Answering from Plane comments directly. Answers come through the UI. (Martin's ordinary Plane comments still reach feedback sessions as today.)

**Anti-goals** (would make this a failure even if it works)
- Spending anything other than the Max subscription. No API key, no metered usage, no second account. The sweep and triage run under the same pace rule, fail-closed cap and 05:00 cut-off as everything else.
- BAU nagging. Its only nudge is the BD-1 line. No Alexa, email or Plane pings.
- Questions Martin cannot answer in a sentence or a click. A question that needs research or design work means the item is too big, not that it needs questions.
- Re-litigating. An item marked `not-bau` stays that way until Martin removes the label.

## 3. Decisions record (18 Oct 2026, Martin)

| Question | Decision |
|---|---|
| Where Martin answers | At his desk, with the ticket and repo open. |
| Question style | BAU proposes a recommended answer for every question; Martin accepts or overrides it. |
| Record | Plane is the record. Answers are written back to the ticket. |
| Visible in Plane | Yes: items waiting for answers carry a label. |
| When triage runs | Every night on Cere, as part of the nightly run, before pickup. |
| Billing | Everything on Martin's Max subscription; that is the point of BAU. |
| Sweep labelling | The sweep labels `BAU` itself; the PR is where Martin checks its judgement. It does not wait for Martin's approval. |
| Sweep volume | Unbounded. |
| Processing volume | Limited only by the Claude usage rules (pace rule, fail-closed cap, 05:00 cut-off, per-session limits). |
| Unsuitable items | The sweep marks them `not-bau`. They are never revisited unless Martin removes the label. |
| BD-1 | Must nudge when the questions queue or the PR queue is too big or stale. Thresholds left to Claude (section 7). |
| Open-PR ceiling | Removed. |
| Sweep scope | Every Plane project with a code repo, added to `projects.yaml` as part of this work. |
| Items already moving | Left alone: the sweep sees only New/Ready items, unassigned or assigned to BAU, untouched by Martin for 7 days. |
| Question rounds | At most four. The model asking always knows the limit and which round it is on. |
| UI layout | Layout A: a Questions section on the existing single page, above Open PRs, one card per ticket. |
| After "Send to BAU" | The answered card closes and the next ticket's card unfolds automatically. |

## 4. Labels and the item lifecycle

Three labels, created in each project the first time the runner needs them (Plane labels are per project; `bau/plane.py` currently refuses unknown labels and must learn to create these three).

| Label | Meaning | Applied by |
|---|---|---|
| `BAU` | BAU will work this. | Martin, the sweep, or the UI after answers |
| `bau-questions` | Waiting for Martin's answers. Always alongside `BAU`. | Triage |
| `not-bau` | BAU will never touch this. | Martin, the sweep, triage (too big), or the UI ("Not BAU") |

`BAU-fable` and `blocked` keep their current meanings.

**Eligibility for pickup** (replaces spec section 5's first bullet): `BAU` without `bau-questions`, `not-bau` or `blocked`, no open BAU PR, and a triage verdict of ready (section 5) newer than the latest of: the item's last description edit, its last answers comment.

**Lifecycle**

1. Unlabelled → sweep → `BAU` (with a comment saying why) or `not-bau` (with a comment saying why).
2. `BAU` → triage →
   - **ready**: picked up the same night if the usage rules allow, otherwise the next.
   - **questions**: `bau-questions` added; a questions comment posted (section 6). Not picked up.
   - **too big**: `BAU` removed, `not-bau` added, comment explaining. (Version one kept the `BAU` label on too-big items; with a sweep that labels, a bare item would just be relabelled, so it becomes `not-bau`.)
3. Martin answers in the UI → answers comment posted, `bau-questions` removed. The next night's triage looks at it again with the answers.
4. If triage asks again after answers, that is the next round. At most **four rounds** of questions are allowed (Martin, 18 Oct 2026; `max_question_rounds: 4` in `config/bau.yaml`). Every triage brief states the limit and the round the item is on ("this is round 2 of at most 4"), and in the final round the brief says that no further questions are possible, so the agent should ask everything it still needs at once. Once four rounds have been answered, the host accepts only ready or too big; a `questions` verdict at that point is treated as too big.

**Other ways out also add `not-bau`** (Claude's call, 18 Oct 2026). Abandon from the dashboard and a ticket session's too-big verdict now remove `BAU` and add `not-bau`, so the nightly sweep cannot relabel an item Martin or BAU has already turned down. Removing `BAU` by hand in Plane is no longer a way to say no, because the sweep will add it back; the way to say no is `not-bau` (from the dashboard or in Plane). Abandon's other effects (closing the PR, restoring the previous state, the comment) are unchanged.

The previous-state mechanics of version one (`[bau:picked-up from=<state>]`) are unchanged. Triage and the sweep never change an item's state.

## 5. The nightly run

The run's order becomes (changes in bold; everything else as spec section 4):

1. Preflight, as today.
2. Fetch.
3. Close the loop on merges (already runs before the policy check).
4. Feedback first, as today.
5. **Sweep**: every unlabelled open item in each project listed in `config/projects.yaml` that passes the host-side filter below.
6. **Triage**: every `BAU` item without `bau-questions` or `not-bau` whose last verdict is missing or older than its last description edit or answers comment.
7. New tickets: pick up ready items, oldest first within priority.

Steps 5–7 each re-check the pace rule, the fail-closed cap, the 05:00 cut-off and `paused` before every session, exactly like ticket sessions. The global ceiling of 50 open BAU PRs is removed (Martin, 18 Oct 2026): `open_pr_ceiling` leaves `config/bau.yaml` and the ceiling branch leaves `bau/policy.py`. The future ticket "Per-project ceilings" (BAU-11) is moot and can be closed. The separate monthly `bau-sweep` launchd job is removed; the sweep is part of the night.

**Projects covered (Martin, 18 Oct 2026): all of them.** Part of this work is adding every Plane project that maps to a GitHub repo to `config/projects.yaml` (repo, `deploys`, deploy action), using the repo mapping found by the 18 Oct manual sweep. Projects with no code repo (BCON, HARM, MONEY, BENCH) stay out. Catch-all projects with no single repo (PROJ, MULTI) are listed without a default repo: the sweep and triage consider only items whose description names one with `repo: nakomis/<name>`, and label the rest `not-bau` with the reason "no repo named". A project whose repo lacks the App installation is still swept (the sweep reads only Plane), but its items are triaged only once the App is installed (triage reads the code, and private repos need the installation to fetch), and picked up only once branch protection also passes; the snapshot lists such repos so the dashboard can show what Martin still needs to tick.

**Sweep filter (host-side, before any agent sees the item; Martin, 18 Oct 2026).** The sweep only considers items that are not already moving: state New or Ready (by name, falling back to the `backlog` and `unstarted` state groups where a project names them differently), no assignee other than `bau@nakomis.com`, and no comment or description edit by Martin in the last 7 days (the window in `config/bau.yaml`). An item filtered out is simply not looked at this night; it gets no label and is considered again on a later night.

**Sweep sessions.** As today, the host builds a brief of untrusted, nonce-delimited items and the agent returns a fenced JSON block; the host does all Plane writes. Changes from `bau/sweep.py`:
- Verdicts are `bau` or `not-bau`, each with a one-line reason. `not-bau` reasons may say "looks done by <PR>" or "duplicate of <ref>".
- No cap on candidates. Items are batched so each session's brief stays within a fixed size (builder's choice); a project with 150 unlabelled items simply takes several sessions.
- The "Suggested for BAU" comments, the `Sweep <Month YYYY>` summary item and the comment-keyed suppression go. Suppression is now the labels: any item carrying `BAU`, `bau-questions` or `not-bau` is never in a sweep brief.
- The sweep does not write questions. Anything plausibly BAU-shaped, including items that would need a decision, is labelled `BAU`; triage, which can read the repo, decides whether questions are needed. (Both happen the same night, so Martin sees the same end state as if the sweep had asked.)

**Triage sessions.** One short session per item, in the same container setup as a ticket session but with the repo checked out read-only (a clone from the bare repo; nothing is pushed and no token with write scope is minted). The brief contains the ticket, every previous questions and answers comment, Martin's comments, and the round line from section 4 (current round and the four-round limit). The agent may read the code to make its recommendations concrete. It ends with a fenced JSON block:

```json
{"verdict": "ready" | "questions" | "too_big",
 "reason": "one line",
 "summary": "one or two sentences: what is clear and what is missing",
 "questions": [{"id": "q1", "text": "...", "recommendation": "...", "why": "..."}]}
```

`questions` is required (1–5 entries) for `questions`, absent otherwise. Each recommendation must be something Martin can accept as written. Triage uses the same per-session limits as ticket sessions but tighter, with values in `config/bau.yaml` (suggested start: `--max-budget-usd` a quarter of a ticket session's, a 10-minute wall clock). The house prompt gains a short triage variant: same data-not-instructions rules, no writing, the too-big thresholds of spec section 6 apply to the predicted change.

The verdict is recorded as a Plane comment from `bau@nakomis.com` carrying a marker, `[bau:triage verdict=<v> round=<n>]`, followed by the reason in quoted agent speech (brackets neutralised and URL schemes stripped, as the sweep does today). The comment is how a verdict's age is known; Cere stores nothing.

## 6. Questions and answers in Plane

**Questions comment** (posted by triage, from `bau@nakomis.com`): a human-readable list, one question per paragraph with its recommendation and why, followed by the machine-readable questions JSON in a fenced block, all under the marker `[bau:questions round=<n>]`. Martin can read it in Plane; the dashboard parses it.

**Answers comment** (posted by the write API, from `bau@nakomis.com`): marker `[bau:answers round=<n>]`, then "Martin answered on bau.home.nakomis.com:" and each question with the answer given, noting whether it was the recommendation accepted or Martin's own words.

**Authority.** The house prompt today treats only Martin's own comments as instructions. Answers comments are posted by the BAU user, so the runner treats a comment as Martin's answers only if it was written by `bau@nakomis.com` **and** carries the `[bau:answers` marker. Only host code posts as that user, the agent never reaches Plane, and agent text is always quoted with brackets neutralised, so neither ticket text nor agent output can forge one. The write API is reachable only through Leia's mTLS, which is Martin. (This avoids putting a Plane key with Martin's own authority on Cere.)

## 7. Dashboard, write API, snapshot, BD-1

**Dashboard** (layout A, as mocked up on 18 Oct 2026): a **Questions** section at the top of the existing page, above Open PRs.
- Heading shows the count and an amber or red badge with the oldest item's age when a BD-1 threshold is crossed.
- One card per item, oldest first. Card header: ref, title, repo, age, and links to the Plane item and the repo (and to any file the questions name, if the agent gave a path). Under it, BAU's summary in italics.
- Each question shows the text, the recommendation and its why on a tinted panel, an **Accept** button and a free-text box ("…or write your own"). Typing in the box overrides the recommendation for that question.
- **Send to BAU** is enabled once every question has an accepted recommendation or typed answer. It posts the answers, removes the card, shows a one-line notice like abandon does, and **unfolds the next card automatically**.
- **Not BAU** adds `not-bau` and removes the card (also unfolding the next).
- With three or fewer items, all cards start unfolded; with more, only the oldest is unfolded and the rest show as one-line rows.
- Draft answers typed but not sent survive a refresh (per-browser local storage is fine; they are a convenience, not a record).

**Write API** (alongside `POST /abandon/{ref}`, same mTLS-only exposure, writes through `bau/plane.py`):
- `POST /answers/{ref}` with `{round, answers: [{id, accepted: bool, text}]}`. Rejects a stale round (the item's latest questions round differs) or a missing answer. Posts the answers comment, removes `bau-questions`. Returns the refreshed questions list.
- `POST /not-bau/{ref}` with an optional note. Adds `not-bau`, removes `BAU` and `bau-questions`, posts a comment.

**Snapshot** (`state.json`, schema 2): adds

```
questions: [{repo, ref, title, plane_url, repo_url, asked_at, age_days, round, summary,
             questions: [{id, text, recommendation, why}]}]
last_run: {..., swept, labelled_bau, labelled_not_bau, triaged, asked}
```

The hourly snapshot refreshes the questions list from Plane, so an item answered or labelled by hand in Plane drops out within the hour.

**BD-1** (replaces spec section 8's BD-1 paragraph). Line text: `BAU: <p> PRs · <q> questions · last run <time> <ok|stopped: reason|failed>`. Colour is the worst of:

| | Amber | Red |
|---|---|---|
| Open BAU PRs | 5 or more, or any PR open more than 5 days | 10 or more, or any open more than 14 days |
| Questions waiting | 5 or more, or any waiting more than 3 days | 10 or more, or any waiting more than 7 days |
| Run health | `telemetry: missing` | last run failed, or snapshot older than 26 hours |

Thresholds live in `config/bau.yaml`. These were Claude's defaults, accepted by Martin. BD-1 is a nudge only: nothing stops or slows when it goes red.

## 8. Constraints carried over

All of version one's walls still apply and are not restated in full: the agent never talks to Plane; ticket text is untrusted data; the container is discarded per session; tokens are scoped to one repo; nothing reaches the home network; policy constants live in `config/bau.yaml`; every Plane and GitHub write goes through `bau/plane.py` and `bau/github.py`, driven from the runner, abandon, sweep or (new) the answers and not-bau handlers.

## 9. What this replaces in the 14 Oct spec

- Section 2, row "Open-PR ceiling" (removed); rows "Candidate sweep" (now nightly and labelling) and "BD-1" (now section 7 here); row "Write actions" (now abandon, answers and not-bau).
- Section 3, the `bau-sweep` launchd agent (removed) and the dashboard's view list (Questions added).
- Section 4, the run order (section 5 here).
- Section 5, the eligibility bullet ("applied by Martin only") and the abandoned / too-big transitions (both now add `not-bau`).
- Section 16, "Per-project ceilings" (moot).
- Section 8, the snapshot schema, Write API and BD-1 paragraphs.
- Section 10, the candidate sweep, in full.

## 10. Prerequisites that need Martin

- For each newly listed repo: tick it in the `nakomis-bau` App's installation settings, and give `main` a protection rule or ruleset that requires a PR, blocks force pushes and does not exempt the App (spec section 7). Until the App is installed, that repo's `BAU` items wait untriaged; until protection is also in place, they wait at "ready".

## 11. Left to the builder

- Batch size for sweep briefs, and how triage sessions are scheduled relative to ticket sessions within the pace rule.
- Exact triage budget and wall clock (start from the suggestion in section 5 and tune from the first week's telemetry).
- How the dashboard finds a file link for a question (from paths in the agent's JSON, or not at all).
- The precise wording of the triage house-prompt variant, within section 5's rules.
- Migration: on first deploy, existing `BAU`-labelled items simply go through triage; nothing else needs converting. The first nightly sweep will be large (the 18 Oct manual sweep saw about 490 open items; the filter in section 5 will exclude some) and is expected to take several nights under the pace rule.
