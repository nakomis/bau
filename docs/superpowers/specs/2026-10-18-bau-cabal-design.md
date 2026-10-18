# BAU Cabal requests: design

Date: 18 October 2026
Status: approved by Martin 18 Oct 2026 (session "bau-pipeline", 18 Oct 2026). Plane: BAU-23.
Builds on: `2026-10-14-bau-design.md` and `2026-10-18-bau-questions-design.md` (BAU-21: labels, marker comments, the Questions section, the answers API). Where this document and those disagree, this one wins for Cabal requests only.

## 1. Intent

The Cabal (`nakomis/mcps/cabal-mcp`) fans one prompt out to several models (Bedrock, Azure Foundry, Gemini, Anthropic) and returns every answer side by side, unsynthesised. Martin uses it whenever independent opinions beat one: design trade-offs, "is this a good idea", reviewing reasoning. BAU agents should be able to use it too.

Unlike everything else BAU does, the Cabal costs real money: each call is metered on the providers' APIs, about 20p a time. BAU itself only spends spare Claude Max allowance. So for now every Cabal call is approved by Martin; later a BAU-wide spending cap replaces his approval.

## 2. Goals, non-goals, anti-goals

**Goals**
- A BAU agent, at any point in a session (mid-ticket, or reviewing its own finished PR), can ask the Cabal a question or request a Cabal review. One mechanism serves both.
- The request waits in a **Cabal** queue on `bau.home.nakomis.com`. Martin can edit what will be sent, then approve it, or comment and deny it.
- Approval runs the Cabal immediately. Answers (or Martin's comment) go back to the ticket, and the ticket returns to the normal queue; the next session has them in its brief.
- Every call's cost is recorded centrally, so a spending cap is a small later change.
- The provider API keys never live on Cere.

**Non-goals**
- Auto-approval. It comes later with the spending cap, and when it does, the editing and commenting steps disappear.
- Synthesising the Cabal's answers. The agent gets them raw, as Martin does.
- More than one open Cabal request per ticket.

**Anti-goals**
- Any Cabal call Martin has not approved (until the cap exists).
- Provider keys on Cere or in the agent container.
- Losing the agent's work when it stops to ask.

## 3. Decisions record (18 Oct 2026, Martin)

| Question | Decision |
|---|---|
| When an agent may ask | Either mid-ticket (a question) or on a finished PR (a review request). |
| Why approval | Cost: the Cabal is metered. Approval will later be replaced by a BAU-wide spending cap. |
| When it runs | Immediately on approval; there is no reason to wait for the night. |
| Where it runs | An AWS Lambda that reads the provider keys from Parameter Store. Cere invokes it using IAM Roles Anywhere with Cere's certificate, following the pattern Cal already uses (home CA trust anchor, role pinned to the certificate CN, no static AWS keys). |
| Editing | Martin can edit the request before it is sent. |
| Denying | Martin can comment before denying; he may answer the question himself. |
| Certificate | The Cabal role trusts Cere's existing client certificate (`~/.config/bau/cere.crt.pem`, the one used for Plane mTLS); no second certificate. |

## 4. The agent's side

- The house prompt (and the triage variant does NOT get this; triage asks Martin, not the Cabal) gains a rule: if an independent multi-model opinion would materially help, the agent may end its session with a final line `BAU_CABAL: <one-line summary>` preceded by a fenced JSON block `{"kind": "question" | "review", "prompt": "<what to send>"}`. The prompt must be self-contained: the Cabal sees nothing else (no repo access), so the agent includes the code, diff excerpt or options it wants judged.
- Before stopping it commits and pushes its work so far. Mid-ticket with no PR yet, it opens the PR as a draft (titled as usual) so the next session resumes from the branch rather than from main.
- Limits (in `config/bau.yaml`): `cabal_prompt_max_chars` (suggested 30000) and `cabal_requests_per_ticket` (suggested 2). A request over the size limit, or beyond the per-ticket limit, is ignored with a Plane comment and the session's result is handled as a normal end.

## 5. The host's side

- The runner detects `BAU_CABAL:` in the session's final message (alongside the existing `BAU_TOO_BIG:` handling), parses the JSON, and: adds label `bau-cabal`; posts a marker comment `[bau:cabal-request n=<n> data=<base64url JSON>]` (n counts requests on this ticket; the payload is `{kind, prompt, summary, pr_url?}`), with a human-readable copy above the marker. The item stays `BAU`; pickup and feedback skip items carrying `bau-cabal`.
- The snapshot gains `cabal: [{repo, ref, title, plane_url, pr_url, requested_at, age_days, n, kind, summary, prompt, estimated_cost_gbp}]`. The estimate comes from `cabal`'s own pricing table for the configured providers and the prompt length (builder's choice how precise).
- BD-1: the line gains `· <c> cabal`, and the nudge gains a `cabal` level with thresholds in `config/bau.yaml` (suggested: warn at 3 or any older than 2 days; down at 6 or older than 5 days).

## 6. Martin's side (dashboard)

A **Cabal** section, above Questions, in the same card style (layout A):
- Card header: ref, title, repo, kind (question / review), age, links to Plane and the PR; the agent's one-line summary.
- An editable text area holding the prompt exactly as the agent wrote it, and the estimated cost.
- A comment box.
- **Approve and send**: sends the (possibly edited) prompt. The card shows "asking the Cabal…" and the page stays usable. When the answers arrive the card disappears with a notice (`HOME-501: Cabal answered (£0.21)`); if the call fails the card returns with the error and stays in the queue.
- **Deny**: requires nothing, but if the comment box has text it goes to the agent as Martin's answer.
- Drafts (edited prompt, comment) survive a refresh, as Questions drafts do.

## 7. Write API and Plane records

- `POST /cabal/{ref}/approve` with `{n, prompt, comment?}`: under the run lock, checks the item carries `bau-cabal` and its latest request is `n` (else 409), records the approval, and starts the Lambda call in the background; returns 202. When the call returns, the host posts `[bau:cabal-answers n=<n> data=…]` (each provider's answer and cost, the prompt actually sent, whether it was edited, Martin's comment if any) with a readable rendering above it, then removes `bau-cabal`. On failure it posts nothing and records the error for the dashboard; the label stays.
- `POST /cabal/{ref}/deny` with `{n, comment?}`: posts `[bau:cabal-denied n=<n> data=…]` with Martin's comment (or "Martin declined; use your own judgement"), then removes `bau-cabal`.
- Only worker-authored comments carry these markers (same authority rule as BAU-21). Comments and edited prompts from Martin are cleaned so they cannot forge a marker, but keep their line breaks.
- The next session's brief (ticket or feedback session) includes the request and its answers or denial as owner-provided data: the Cabal's answers are labelled as untrusted model output, Martin's comment and edits as owner text.

## 8. The Cabal Lambda

- One function, `bau-cabal` (name builder's choice), Python, packaging the existing `cabal` library from `cabal-mcp` (its `fanout` and providers) with a secrets backend that reads Parameter Store instead of the macOS keychain. Bedrock is called with the Lambda's own role. Timeout 10 minutes.
- Input `{prompt, system?, providers?, bluntness, max_tokens?}`; output the same shape `ask_all` returns (answers, per-provider cost, notes).
- Every call is logged with its cost to a small table (DynamoDB or similar; builder's choice) keyed by month, so the future spending cap is a read of this month's total. The Lambda can enforce the cap itself when it exists.
- Invoked directly with `lambda:InvokeFunction` (no API Gateway: its 29-second limit is too short).
- Infrastructure as CDK in the repo that owns the shared AWS stacks (`nakomis-infra`, or alongside `cabal-mcp`; builder's choice, noted in the plan), sandbox and production like the other stacks.

**Cere's access (follow Cal's pattern exactly):** the home CA certificate (SSM `/home-certs/<env>/ca-cert-pem`) is the Roles Anywhere trust anchor; a role whose trust policy pins the certificate CN to Cere's, permitted only `lambda:InvokeFunction` on this function; a Roles Anywhere profile for it. Cere gets `aws_signing_helper` and a `credential_process` profile in `~/.aws/config` (as the conversation-memory forwarder does on the Macs; see home-infra `docs/runbooks/conversation-memory-ingest-bootstrap.md`). The trust anchor, profile and role ARNs live in Parameter Store under a `/bau/cabal/` prefix. No static AWS key anywhere on Cere. The Cere playbook installs the helper and writes the profile.

Optionally (not required for BAU-23), the Cabal MCP on Martin's Macs can switch to the same Lambda later, giving one implementation and one cost log.

## 9. What this changes in BAU-21's design

- Labels: adds `bau-cabal` (pickup, feedback, triage and the sweep all skip it; the sweep's own skip list includes it).
- Snapshot schema 3: adds `cabal` and `nudge.cabal`.
- The dashboard gains a Cabal section; BD-1 gains the cabal count.
- Write actions: adds Cabal approve and deny.

## 10. Claude's calls (from the builder check; Martin to confirm while reading)

- **Run lock.** Approve and deny take the run lock only for their Plane writes (seconds), never for the Lambda call, so Martin can approve while the night is running; if the lock is busy for the writes, the API retries for up to 60 seconds before giving up with the error on the card.
- **No double spend, no lost answers.** Each request has an id `<ref>#<n>`. The Lambda stores every result in DynamoDB under that id (30-day TTL) and, given an id it has already answered, returns the stored result without calling the providers again. The host therefore posts answers idempotently: an API restart mid-call, or a crash after the Lambda returned, just means approving again (or the host retrying) fetches the stored answers at no extra cost. "In flight" lives only in the API process's memory; a second approve while one is in flight is refused with 409, and deny is refused until the call settles.
- **Environment.** Cere calls the production Lambda with the production trust anchor; the sandbox stack exists for testing from Martin's Mac and CI.
- **Provider keys.** Reuse the parameters `cabal-mcp` already reads (`/cabal-mcp/<name>`, its existing SSM backend); the Lambda role gets read access on that prefix only, and the Lambda sets no keychain path. Prerequisite 11's "copy keys" becomes "confirm they are in the production account".
- **Logging and privacy.** The Lambda disables Cabal's file trace and `save_dir`; CloudWatch gets metadata only (request id, providers, token counts, cost, durations), never prompt or answer text. Prompt and answers are kept only in the 30-day result store and on the Plane ticket.
- **Unknown cost.** When a provider's cost is unknown, the call is recorded with `cost_incomplete: true`, and the dashboard shows the estimate as "at least £x". The future spending cap will decide how to count these.
- **Providers and framing.** Requests use Cabal's default providers with `bluntness: true`. `kind: review` adds a fixed system prompt framing a code review; `kind: question` adds none. The review system prompt lives in `config/bau.yaml`.
- **Feedback sessions** may also end with `BAU_CABAL`; the PR already exists, so no draft is needed.
- **Per-ticket limit** counts every request (approved or denied); with the suggested limit of 2, a denial still leaves one more.

## 11. Prerequisites that need Martin

- Confirm the provider keys are in the production account's Parameter Store under `/cabal-mcp/`.
- Read the CN from Cere's existing certificate for the role's trust policy (the certificate is reused, per section 3).

## 12. Left to the builder

- The JSON marker payload shapes beyond the fields named here.
- How the estimate is computed.
- The cost table's exact schema.
- Which repo hosts the Lambda's CDK, with a line in the plan saying why.
- Whether the background call on approval is a thread in the API process or a small job runner, provided an API restart mid-call leaves the item in the queue with its label (so it can be approved again).
