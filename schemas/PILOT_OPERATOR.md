# Explicit article-pilot operator guide

**Review and human-merge the PR first. Replace dummy credentials locally only
after merge. Do not execute the live commands while reviewing the PR.** Tests use
fabricated inputs and real SDKs over in-process mocked HTTP, with sockets denied.
No account access, model availability, live tariff or content-quality result has
been verified by those tests.

## Scope and approval

The v1 launcher supports exactly one complete case and three independent
writer → reviewer → revision chains, in this order:

- `gpt-6-luna`, low reasoning effort;
- `gpt-6-sol`, low reasoning effort;
- `claude-sonnet-5`, adaptive thinking, low effort.

At most **nine requests**, concurrency one, no retries/rerolls/fallbacks, 8,192
output-token cap per request, 100,000-byte prompt cap, 120-second request timeout.
These are illustrative pilot choices, not a recommended model ranking. Each
model reviews and revises its own draft; this is not a crossed reviewer study.
The single durable campaign authority caps conservative reservations at **US$50
total across all models and resumes**. Reservations are not invoices or actual
charges. The cap assumes the reviewed standard/global USD tariffs; verify account
routing, applicable multipliers and provider-side spend limits separately.

The transport sends a serialized envelope (including the suite's nested messages,
source/evidence and exact saved parent outputs) as **one native user message**.
It does not reproduce a product's native system/user roles, tool configuration or
production pipeline verbatim. Review this transport difference before approving
source egress. It has no publication integration.

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
must contain exactly one case with all three role inputs. Supply its external
writer and reviewer JSON schemas explicitly; no private templates, raw source
material or machine-specific paths belong in the public repository.

Configuration:

- Set `organization` and `project` to the intended lab OpenAI route, matching the
  explicit credential file. `account_route` is an operator label for the approved
  lab accounts (including the Anthropic workspace/key assignment), not automatic
  account discovery. Do not use production credentials.
- `body_words` is an inclusive whitespace-token range for `body`, excluding a
  conventional trailing `## Sources`, `## References` or `Sources:` section.
- Set `review_item_ids` to the exact IDs required by the private reviewer schema.
  The example IDs/range are illustrative; check them against your frozen packet.
- Writer/revision must return JSON with exactly `title`, `body`, `meta_title`,
  `meta_description`, `tags`; reviewer must return exactly `results` and
  `factual_issues`. Explicit schemas enforce field types and details. Schemas
  must use draft-2020-12-compatible keywords and contain **no `$ref`,
  `$dynamicRef` or `$recursiveRef`**; nothing fetches remote schemas.

Tariff attestation:

- The example deliberately has `reviewed: false`. It cannot authorize dispatch.
- Independently check current official prices, exact model IDs, currency,
  standard/global service route, long-context/cache multipliers and your lab
  account terms. Check permission to send this exact source packet to both
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
WRITER_SCHEMA=/path/to/private-packet/writer-output.schema.json
REVIEWER_SCHEMA=/path/to/private-packet/reviewer-output.schema.json
OUTPUT=/path/to/private-runs/new-pilot
PLAN="$OUTPUT/plan.json"

uv run --offline draftbench validate "$SUITE"
uv run --offline draftbench pilot prepare \
  --suite "$SUITE" --config "$CONFIG" --tariff "$TARIFF" \
  --writer-schema "$WRITER_SCHEMA" --reviewer-schema "$REVIEWER_SCHEMA" \
  --output "$OUTPUT" --plan "$PLAN"
uv run --offline draftbench pilot preflight --plan "$PLAN"
```

`prepare` exclusively creates a 0700 private output root, one 0600 campaign
SQLite database and a 0600 `plan.json`. It never loads `.env`, imports a provider
SDK or dispatches. It can prepare an unreviewed tariff for inspection, but that
plan cannot run. Finish the attestation **before** making the final live plan.
Do not recreate a campaign that has any reservations to reset its budget.

Privately inspect `plan.json`: exact input corpus and source/evidence custody,
external schemas, code fingerprint, dependency versions, model policies, routes,
prices, all limits, campaign identity and exact child-run paths. The plan contains
private content; do not paste it into an issue or attach it to the PR. Its digest
is not a secret. Input files must remain unchanged and not be concurrently
writable by another operator during preparation/run.

Preflight recomputes the entire scope and rejects changes, including altered
source hashes, configuration, schema, tariff, code, dependency versions or
campaign identity. It does not grant approval and does not claim account access.
Copy the **exact** displayed `approval_digest` only after personally reviewing
that scope and authorizing source egress and the bounded spend. Do not pipe
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

**This command sends private source material to providers and may incur charges.**
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
request. Malformed JSON, incorrect shape, missing/repeated reviewer IDs, short or
long bodies, front matter, truncated provider output, failures and uncertain
attempts stop the pilot. No repair, reroll, rewriting the suite or replacing a
bad result is automatic. A successful provider ledger is only native transport
completion; the **pilot** may still stop on its stricter output checks.

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
outputs, and never retries uncertain/failed/malformed work. Completed work is
not repeated. A stranded campaign reservation is not refunded. Stop and preserve
the evidence if the ledger, plan or database is missing/corrupt. Never copy,
restore, replace or delete the campaign as a recovery shortcut.

Exit 0 means the command completed (for run/resume, all three chains met the
mechanical output checks); exit 3 means stopped/incomplete; exit 2 means refused
or invalid inputs/state. Errors deliberately omit sensitive details. On refusal,
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
quote fidelity and critique quality remain **unassessed**. Mechanical completion
is not a semantic pass, production publication gate or model-ranking claim.
Nothing in this workflow automatically publishes, promotes or deploys content.
