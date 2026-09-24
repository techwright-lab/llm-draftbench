# Explicit article-pilot operator guide

**Review and human-merge the PR first. Replace dummy credentials locally only
after merge. Do not execute the live commands while reviewing the PR.** Tests use
fabricated inputs and real SDKs over in-process mocked HTTP, with sockets denied.
No account access, model availability, live tariff or content-quality result has
been verified by those tests.

## Scope and approval

The launcher measures how **TrustGrowth's own prompts** perform on other models.
It supports exactly one complete case exported by TrustGrowth's
`draftbench:export` and three independent chains of four calls, in this order:

- `gpt-6-luna`, low reasoning effort;
- `gpt-6-sol`, low reasoning effort;
- `claude-sonnet-5`, adaptive thinking, low effort.

Each chain is writer → reviewer → revision → reviewer of the revision. At most
**twelve requests** (four per model), concurrency one, no
retries/rerolls/fallbacks, 16,000 output-token cap per request (TrustGrowth's
writer value), 100,000-byte prompt cap, 300-second request timeout. These are
illustrative pilot choices, not a recommended model ranking. Each model reviews
and revises its own draft; this is not a crossed reviewer study. The single
durable campaign authority caps conservative reservations at **US$50 total
across all models and resumes**. The twelve calls reserve US$37.01 (Luna 0.97,
Sol 19.40, Sonnet 16.64; per call 0.2425, 4.85 and 4.16). Reservations are not
invoices or actual charges. The cap assumes the reviewed standard/global USD
tariffs; verify account routing, applicable multipliers and provider-side spend
limits separately.

### What is sent

The export carries, per role, the exact offline-rendered TrustGrowth messages:
one system and one user message. They are sent **as native roles**:

- OpenAI, through the Responses API as TrustGrowth does: `instructions` is the
  system message, `input` is the user message, `text.format` is a strict
  `json_schema`, `reasoning.effort=low`, `max_output_tokens=16000`,
  `store=false`.
- Anthropic: a `system` text block, one user text block,
  `output_config.format` json_schema structured output, `output_config.effort=low`,
  adaptive thinking, `max_tokens=16000`.

The export has no schema or request settings. The lab pins TrustGrowth's
`SeoContentSchema` (writer, revision) and `ReviewLedgerSchema` (reviewer) and the
settings above from TrustGrowth revision
`1937049452c757dc346da01017ac50866fbeb169` in
`draftbench.adapters.replay_contract`. `prepare` refuses a case whose
`prompt_version` is not a prefix of that revision (`tg_revision_mismatch`); a
newer export needs a reviewed lab update first. Saved outputs are still validated
against the same schemas after each call.

Per role:

- **Writer**: the exported writer messages, unchanged.
- **Reviewer**: the exported TrustGrowth reviewer payload with the lab draft
  substituted in. The exported TrustGrowth draft body must appear exactly once
  between `## The draft` and `## Items`, and the `Title:` /
  `Meta description:` promise lines exactly once before it; otherwise the call is
  refused (`reviewer_anchor_mismatch`) instead of reviewing the wrong text. The
  meta description line follows TrustGrowth's template: present only when the
  lab draft's meta description is not blank. Sibling drafts, profile and item
  questions stay as exported. The second reviewer reviews the revision the
  same way.
- **Revision**: the prompt rendered by TrustGrowth for this lab draft and lab
  review, from the TrustGrowth review replay (below). The lab never builds its
  own revision prompt.

The private source sidecar, evidence files, stored TrustGrowth reviews and
evaluator labels are **never** sent to a provider; TrustGrowth sends no source
documents either. A role input that names evidence is refused
(`evidence_not_supported`). TrustGrowth's Ruby review checks are not ported;
the full reviewer (Ruby checks + LLM ledger + RubricV4) runs in TrustGrowth.

Known differences from TrustGrowth: its reviewer call sends no output cap or
effort (provider defaults), while the lab uses 16,000 and low for all four
calls; RubyLLM's Responses payload also asks for
`include=["reasoning.encrypted_content"]`, which the lab omits because it never
chains turns; TrustGrowth's silent provider fallback does not exist here. There
is no publication integration.

## 1. Install the merged revision and configure private inputs

From the merged checkout (dependency installation may use package-index access):

```sh
uv sync --locked --extra pilot
uv run --offline draftbench pilot --help
```

Alternatively install the reviewed wheel with its `pilot` extra. Keep the same
installed package/dependency versions through preparation and resume. `uv
--offline` only disables package-index access; it does **not** sandbox a live
`pilot run` command.

Use an existing private directory outside Git for configuration. Copy
`examples/pilot/config.example.json` and `tariff.example.json` there, without
replacing any existing file. Edit the copies, not the frozen suite. The suite
must contain exactly one case with all three role inputs, each exactly one
system and one user message. No private templates, raw source material or
machine-specific paths belong in the public repository.

Configuration:

- Set `organization` and `project` to the intended lab OpenAI route, matching the
  explicit credential file. `account_route` is an operator label for the approved
  lab accounts (including the Anthropic workspace/key assignment), not automatic
  account discovery. Do not use production credentials.
- The config format is `article-pilot-v2` with exactly `format`,
  `account_route`, `organization` and `project`. There is no length range or
  item list to configure: the reviewer item IDs are read from the exported
  reviewer message (`- item_id: …` lines under `## Items`), and the target length
  from `- Target Word Count: N` in the exported writer message.
- Writer/revision must return JSON valid against the pinned `SeoContentSchema`
  (`title`, `body`, `meta_title`, `meta_description`, `tags`); reviewers against
  `ReviewLedgerSchema` (`results`, `factual_issues`) with exactly the exported
  item IDs.

Tariff attestation:

- The example deliberately has `reviewed: false`. It cannot authorize dispatch.
- Independently check current official prices, exact model IDs, currency,
  standard/global service route, long-context/cache multipliers and your lab
  account terms. Check permission to send these exact exported messages to both
  providers. Review account access/billing in provider consoles; the offline
  preflight cannot prove them.
- Record actual `reviewed_by`, `reviewed_at` (UTC), source URLs and a meaningful
  `scope` describing the checked account/route and tariff applicability. Archive
  review evidence privately. Only then set `reviewed` to `true`.
- Rates are `[input, output]` USD per million tokens. The v1 contract fixes
  Luna `["0.1","0.5"]`, Sol and Sonnet `["2","10"]`. If applicable prices or
  routes differ, **stop**; do not fabricate a review or edit the evidence to
  bypass the mismatch. A tariff-contract update needs code review.

This is a **local operator attestation**, not provider-authenticated proof. A
JSON boolean alone does not verify a price. The launcher binds its exact content
and requires a separately entered approval digest.

## 2. Prepare and inspect offline (no credentials or network)

Set these non-secret path placeholders in your shell. Choose an existing private
parent and a **new** output directory. Do not move/rename the result after review.

```sh
SUITE=/path/to/private-packet/suite.json
CONFIG=/path/to/private-config/config.json
TARIFF=/path/to/private-config/tariff.json
OUTPUT=/path/to/private-runs/new-pilot
PLAN="$OUTPUT/plan.json"

uv run --offline draftbench validate "$SUITE"
uv run --offline draftbench pilot prepare \
  --suite "$SUITE" --config "$CONFIG" --tariff "$TARIFF" \
  --output "$OUTPUT" --plan "$PLAN"
uv run --offline draftbench pilot preflight --plan "$PLAN"
```

`prepare` exclusively creates a 0700 private output root, one 0600 campaign
SQLite database and a 0600 `plan.json`. It never loads `.env`, imports a provider
SDK or dispatches. It can prepare an unreviewed tariff for inspection, but that
plan cannot run. Finish the attestation **before** making the final live plan.
Do not recreate a campaign that has any reservations to reset its budget.

Privately inspect `plan.json`: exact exported messages and suite custody,
pinned schemas and TrustGrowth revision, reviewer item IDs, target words, code
fingerprint, dependency versions, model policies, routes,
prices, all limits, campaign identity and exact child-run paths. The plan contains
private content; do not paste it into an issue or attach it to the PR. Its digest
is not a secret. Input files must remain unchanged and not be concurrently
writable by another operator during preparation/run.

Preflight recomputes the entire scope and rejects changes, including altered
source hashes, configuration, tariff, code (including pinned schemas),
dependency versions or campaign identity. It does not grant approval and does not claim account access.
Copy the **exact** displayed `approval_digest` only after personally reviewing
that scope and authorizing egress of the exported messages and the bounded
spend. Do not pipe
preflight into run or programmatically auto-approve it.

## 3. Replace the local dummy credential file after merge

`.env.example` documents the four allowed names. If `.env` already exists,
**edit it locally; do not copy over it**. For a new file, create it with mode 0600
and paste values using a trusted local editor/password-manager workflow. Never
put secrets in commands, shell history, chat, issues or plan/config files.

```sh
# Only if the file does not already exist; noclobber prevents replacement.
(umask 077; set -C; : > /path/to/private-credentials/.env)
chmod 600 /path/to/private-credentials/.env
```

The syntax is literal `NAME=value`, plus blank lines and whole-line `#` comments.
All four fields are required. No quotes, spaces in values, `export`, interpolation,
inline comments, multiline values or shell commands. Never `source` this file.
The loader rejects unknown/duplicate fields, empty/dummy placeholders, symbolic
links (including parent components), hard links, wrong owner and any mode other
than 0600. It reads only the path passed to `--env-file`, never ambient provider
variables or automatically discovered dotenv files. The existing ignored root
`.env` can also be explicitly selected if it meets these rules; it is never
packaged. OpenAI org/project must match the approved config exactly.

## 4. Explicit live run — post-merge, operator-authorized only

**This command sends the exported private messages and lab drafts to providers
and may incur charges.**
Do not run it during offline PR validation. Enter the reviewed digest manually:

```sh
uv run --offline draftbench pilot run \
  --plan "$PLAN" --env-file /path/to/private-credentials/.env \
  --approve EXACT_REVIEWED_PREFLIGHT_DIGEST
```

The launcher compares each child-run scope against the approved frozen digest;
it does not install an unconditional approval callback. Credentials are not
written to the plan, run artifacts, reports or errors. A new `run` refuses any
existing child run; it cannot silently start over.

After each completed role, validate the exact saved output before the next
request. Malformed JSON, schema-invalid output, missing/repeated reviewer IDs,
front matter, truncated provider output, failures and uncertain attempts stop
the pilot. **Length is not a stop.** The result reports, per model and for the
writer and the revision, `length` = `body_words` (whitespace tokens of `body`,
excluding a trailing `## Sources`, `## References` or `Sources:` section),
`target_words` from the brief and `ratio_to_target` (null when the brief has no
target). No repair, reroll, rewriting the suite or replacing a bad result is
automatic. A successful provider ledger is only native transport completion;
the **pilot** may still stop on its stricter output checks.

### TrustGrowth review replay (between reviewer and revision)

The run pauses for each model after its first reviewer call. It writes a replay
request and returns exit 3 with `"complete": false`,
`"waiting": "tg_review_replay_required"` and `review_replay_requests` (paths
relative to `$OUTPUT`). Nothing is reserved while waiting.

Request, written once per model (a changed rewrite is refused):
`$OUTPUT/review-replay/<model>.draft.request.json`

```json
{
  "format": "draftbench-review-replay-request-v1",
  "case_id": "draft-123",
  "model": "gpt-6-sol",
  "stage": "draft",
  "tg_revision": "1937049452c757dc346da01017ac50866fbeb169",
  "draft": {"title": "…", "body": "…", "meta_title": "…", "meta_description": "…", "tags": ["…"]},
  "draft_sha256": "<sha256 of the exact saved writer output text, UTF-8>",
  "ledger": {"results": [], "factual_issues": []},
  "review_sha256": "<sha256 of the exact saved reviewer output text, UTF-8>"
}
```

`ledger` is the lab model's `ReviewLedgerSchema` answer. The TrustGrowth
no-write replay takes this file, runs the full TrustGrowth reviewer on the lab
draft **using this ledger instead of its own LLM call** (Ruby checks, quote
verification, `LedgerBuilder`, `RubricV4`), renders the revision prompt with
TrustGrowth's own feedback and prior-body rendering, and writes nothing to its
database. Place its response at `$OUTPUT/review-replay/<model>.response.json`:

```json
{
  "format": "trust-growth-review-replay-v1",
  "case_id": "draft-123",
  "tg_revision": "<TrustGrowth commit, 7-40 hex>",
  "draft_sha256": "<echoed from the request>",
  "review_sha256": "<echoed from the request>",
  "review": {"…": "TrustGrowth review report for the lab draft"},
  "revision": {"messages": [
    {"role": "system", "content": "<must equal the exported revision system message>"},
    {"role": "user", "content": "<TrustGrowth-rendered revision prompt>"}
  ]}
}
```

Exactly these keys are accepted. `draft_sha256`/`review_sha256` must match the
saved lab outputs and the system message must equal the exported one, otherwise
resume is refused (exit 2). Then run `pilot resume` (section 5). The response is
bound into the revision request's saved custody (its `external_input`), not into
the approval digest; `review` is kept there as evidence but is not sent. Only
`revision.messages` is sent.

After the reviewer of the revision completes, the pilot writes
`$OUTPUT/review-replay/<model>.revision.request.json` (`stage: "revision"`) so
TrustGrowth can produce the final verdict on the revision. No further call is
made.

## 5. Resume, inspect and review without inventing results

After an ordinary interruption, first inspect saved state offline:

```sh
uv run --offline draftbench pilot preflight --plan "$PLAN"
uv run --offline draftbench openai report "$OUTPUT/gpt-6-luna"
uv run --offline draftbench openai report "$OUTPUT/gpt-6-sol"
uv run --offline draftbench anthropic report "$OUTPUT/claude-sonnet-5"
```

Only child directories that were actually started exist. Reporting a missing
one returns an error, not a fabricated result. If the same scope is still
approved, explicitly resume:

```sh
uv run --offline draftbench pilot resume \
  --plan "$PLAN" --env-file /path/to/private-credentials/.env \
  --approve EXACT_REVIEWED_PREFLIGHT_DIGEST
```

Resume uses the original campaign and child ledgers. It recovers in-flight work
to uncertain **without dispatch**, retains all reservations, validates saved
outputs, picks up any review replay response placed since the last run, and
never retries uncertain/failed/malformed work. Completed work is
not repeated. A stranded campaign reservation is not refunded. Stop and preserve
the evidence if the ledger, plan or database is missing/corrupt. Never copy,
restore, replace or delete the campaign as a recovery shortcut.

Exit 0 means the command completed (for run/resume, all three chains met the
mechanical output checks); exit 3 means stopped, incomplete or waiting for the
TrustGrowth review replay; exit 2 means refused or invalid inputs/state. Errors deliberately omit sensitive details. On refusal,
check paths, permissions, explicit fields, installed versions, unchanged inputs,
route/approval equality and tariff review locally—do not print credentials. A
terminal failure needs human diagnosis and a separately reviewed new experiment,
not an automatic paid retry. Reconcile charges against provider billing manually.

Saved provider outputs can be prepared/rendered/replayed without credentials or
SDK execution. Supply an explicit private output-rights declaration (schema in
`schemas/CONTRACT.md`), rather than inferring output rights from source rights:

```sh
uv run --offline draftbench openai prepare "$OUTPUT/gpt-6-luna" \
  --rights /path/to/output-rights.json --output /path/to/new-prepared
uv run --offline draftbench report render \
  --provider-run "$OUTPUT/gpt-6-luna" \
  --bundle /path/to/new-prepared/bundle.json \
  --bindings /path/to/new-prepared/provider-bindings.json \
  --output /path/to/new-private-report
uv run --offline draftbench report replay /path/to/new-private-report \
  --output /path/to/new-replay
```

Use `anthropic prepare` for Sonnet. Open the static local `index.html`. Core report
and replay operations do not load credentials or provider SDKs; only the pilot
launcher requires the extra dependencies. Preserve exact saved outputs for a
later separately blinded human review: factual grounding, usefulness, SEO quality,
quote fidelity and critique quality remain **unassessed**; the TrustGrowth
verdicts from the review replay are recorded evidence, not a lab score. Mechanical completion
is not a semantic pass, production publication gate or model-ranking claim.
Nothing in this workflow automatically publishes, promotes or deploys content.
