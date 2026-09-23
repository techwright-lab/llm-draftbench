# OpenAI text adapter v1 — offline implementation, live pilot NOT authorized

This adds a real `openai==2.29.0` / `httpx==0.28.1` Chat Completions SDK
transport, verified with **in-process mocked HTTP only**. No provider request,
credential discovery, account access, billing observation or live compatibility
verification has occurred. The legacy provider CLI remains fixture/report-only;
the separate [pilot launcher](PILOT_OPERATOR.md) has an explicit operator-approved
three-model live entry point. Existing fake/Inspect
contracts and their synthetic reports remain separate.

## Supported contract, not a model recommendation

Only these explicitly supplied dated snapshots are admitted:
`gpt-4.1-2025-04-14`, `gpt-4.1-mini-2025-04-14`,
`gpt-4.1-nano-2025-04-14`. No default model, alias, Responses API, Azure route,
reasoning-effort selection, tools, images/audio, prediction, streaming, batch,
priority/flex pricing, automatic fallback or reroll is implemented.

Official model pages were read to verify each snapshot, **1,047,576 context
window** and **32,768 maximum output**:
- https://developers.openai.com/api/docs/models/gpt-4.1
- https://developers.openai.com/api/docs/models/gpt-4.1-mini
- https://developers.openai.com/api/docs/models/gpt-4.1-nano

The installed SDK's constructor and `chat.completions.create` signature and
`CompletionCreateParamsBase.max_completion_tokens` documentation were inspected.
The call uses `client.chat.completions.with_raw_response.create(...)` and `.parse()`;
raw response JSON is preserved as well, avoiding SDK coercion of invalid usage.
The SDK documents `max_completion_tokens` as including visible and reasoning
tokens. GPT-4.1 is not a reasoning-effort model; defensive accounting treats any
reported reasoning tokens as already included in `completion_tokens`, never adds
them again. These checks do not establish that an account has access to a model.

The fixed endpoint is `https://api.openai.com/v1`, single text user message,
`max_completion_tokens=<explicit cap>`, `n=1`, `stream=false`, `store=false`,
`service_tier=default`. The message is the exact canonical serialized role-input
and parent-output envelope, not a claim to reproduce historical message framing.
UTF-8 source/evidence files are hash-verified and materialized in that envelope.
Evaluator labels, producer metadata and rights text are excluded from prompts.
The SDK receives `max_retries=0`; HTTPX disables redirects and environment proxies.
429 and read-timeout tests observe exactly one transport attempt. Network timeout
is an HTTP operation timeout, **not** provider cancellation or a whole-job deadline.

## Authorization / custody

`approval_scope(suite_path, output, policy)` returns private review material and a
SHA-256 binding; it does **not** approve anything. A separately trusted embedding
host must obtain explicit human authorization for that exact scope and supply
`approve(binding)` backed by its approved digest, plus an explicit API key string
to `run_openai` / `resume_openai`. Never approve by blindly returning true or by
accepting an approval field from suite/config data. The legacy adapter has no
ambient credential lookup or automatic model selection. The separate pilot
launcher supplies this callback only after exact operator-digest approval and
strict explicit credential loading.
The host is trusted Python code, not a sandbox against code that rewrites internals.
A directly constructed `DispatchAuthorization` is invalid; the runner issues a
single-use capability only after authorization, admission and durable in-flight
transition. Resume reauthorizes the frozen scope; uncertain work is never retried.

The binding covers SDK/API route, exact model, account route label, organization,
project, currency, prices and price units, all run limits, plan/input/source bytes,
suite/case identities, parent propagation protocol, provenance and output directory.
The API key itself is never stored. The caller must ensure that the supplied key
belongs to the approved lab-owned organization/project; this code neither probes
account ownership nor treats a route label as proof of it. Organization and project
headers are explicit. The fixture key is an in-process placeholder, not a secret.
`OPENAI_*` values are not discovered even by the SDK (including webhook secret).

The [five-model contract](FIVE_MODEL_CONTRACT.md) and [operator guide](PILOT_OPERATOR.md)
now specify the narrow pilot models, USD campaign and bounds. Current applicable
tariffs, lab credentials/account access, corpus egress and independent exact-scope
approval remain operator gates, not claims established by mocked tests.

## Conservative admission and billing uncertainty

`OpenAIPolicy` requires positive decimal-string prices, explicit ISO-style currency
code, `currency_per_million_tokens` units and a spend cap in that currency. Pricing
is an operator assertion requiring independent verification, not an SDK quote.
No cached-token discount is guessed. For each admitted attempt reserve:

```
token_units = entire_model_context_window + max_output_tokens
cost = (entire_model_context_window * input_per_million
        + max_output_tokens * output_per_million) / 1_000_000
```

Reserving the **whole model context**, rather than an invented bytes-to-token
conversion, intentionally over-reserves input/framing. This assumes the documented
text model context bound and supplied prices hold. It is not universal OpenAI
accounting, an invoice, a guarantee against provider pricing changes/taxes/other
account traffic, or a substitute for an account-side spend limit. Cached input is
charged at full supplied input rate for the upper estimate; reasoning is included
once. Every non-null detail counter must have exact non-boolean integer wire
type: `false`, `true`, `0.0` and `"0"` are invalid even when SDK parsing coerces
them. Detail containers must be objects or null. Cached/reasoning counts are
bounded by their corresponding input/output total; unsupported detail counts
(including audio/prediction) must be integer zero or null. Nonzero
audio/prediction counters, malformed/negative/non-integer counters,
usage above admitted context/output limits and inconsistent totals are uncertain.
Missing usage remains null; live charge status is unknown. Known usage yields only
an unreconciled conservative estimate, never a settled charge.

`Ledger.reserve` invokes provider admission within the same SQLite transaction that
appends/fsyncs the reservation. Whole-run lock and immutable manifest separately
serialize the executor. Failed/limited/uncertain attempts consume their entire
reservation permanently. Crashes after `in_flight` become uncertain; crashes after
`result_saved` may finalize the saved artifact without generating again. Caps cannot
be raised on resume. HTTP errors have no saved exception text/body or credentials;
private normalized outcomes/receipts retain unknown billing. Unexpected persistence
errors leave the started reservation in flight for conservative recovery.

## Artifacts, scoring and reports

`draftbench-openai-run-v1` is separate from `draftbench-synthetic-run-v1`.
In-process MockTransport always yields `provenance=fixture`; the real transport mode
records `provider`, never silently relabels provider output as synthetic. Success
artifacts preserve request/served model, native completion ID and HTTP request ID,
exact request, output, parents, raw native JSON, nullable usage, local elapsed time,
and null provider latency (no guessed provider-time header conversion).

Private content-addressed artifacts and terminal receipts use existing private
permissions/fsync/locks. Saved results are validated against native content,
configuration and parents before resume/report. Reports expose aggregate state,
provenance and reservations only, not prompts, labels, rights or raw outputs.
`report_openai` opens SQLite read-only and never recovers it or imports the SDK.

`openai prepare` requires a **new explicit output-rights declaration**, not an
assumption that source rights cover generated text. Each complete writer/revision
output is one verbatim text unit, identified by its work ID; reviewer output is not
misparsed as structured findings. The existing mechanical-only scorer is used,
with unknown semantic reference/acceptability and no gold-label invention.
Provider-derived scoring bundles have purpose `evaluation`; fixture-derived ones
remain `synthetic_infrastructure`. `provider-bindings.json` contains format
`draftbench-openai-scoring-bindings-v1`, `provenance`, `manifest_digest`,
`snapshot_digest`, `bundle_identity`, and `cases` (the work/case/request/result/role,
artifact and scoring-case identities). It is not the legacy synthetic format.

`report render --provider-run RUN --bundle BUNDLE --bindings SIDECAR` reads the
saved ledger **read-only under its existing lock**, verifies manifest policy and
transport, source context, parent chain, native output and usage, then reconstructs
the exact prepared bundle and sidecar. Missing, invalid, substituted or changed
inputs fail closed. A changed run needs fresh preparation. Older sidecars without
`snapshot_digest` must be regenerated, not silently trusted. No provider SDK or
provider generation module is imported by preparation, rendering or replay.

Reports explicitly distinguish `custody=verified_provider_preparation` from
`standalone`, with `provenance=fixture|provider|supplied_artifact`, role panels,
execution state and an honest watermark. Fixture reports never claim model
execution. Reports are exploratory/mechanical, not live compatibility or billing
verification. The private frozen snapshot retains the full manifest (including
policy and source context), request and native result artifacts; exact replay
revalidates their hashes and bindings without access to the original run.

`report render --bundle BUNDLE` is allowed as standalone scoring, **not custody
verification**. Supplying any `--bindings` without a matching run and bundle is an
error, even if the file is missing. Hashes establish internal consistency, not
origin authentication against an owner who replaces all evidence. Output rights
remain a caller declaration, not inferred permission. Nothing here publishes
outputs or rights; provider evidence remains excluded from the public release path.

## Reproduce offline

```
uv sync --locked --extra openai --extra inspect
uv run --offline --extra openai pytest tests/test_openai_provider.py tests/test_openai_authorization.py
uv run --offline --extra openai python examples/openai/smoke.py examples/smoke/suite.json /new/private/smoke
```

The smoke uses a clearly synthetic policy (`XXX` test currency, invented fixture
prices) and blocks socket/DNS/connect creation. It exercises real SDK serialization
through MockTransport, CLI pause/resume, read-only run report, mechanical prepare/
score, static render and exact replay. It never enables a live path.

```
draftbench openai fixture-run suite.json --policy fixture-policy.json --output /new/private/run
draftbench openai fixture-resume /private/run
draftbench openai report /private/run
draftbench openai prepare /private/run --rights output-rights.json --output /new/private/prepared
draftbench score /private/prepared/bundle.json
draftbench report render --provider-run /private/run --bundle /private/prepared/bundle.json --bindings /private/prepared/provider-bindings.json --output /new/private/report
draftbench report replay /private/report --output /new/private/replay
```

SDK packages are optional; saved reporting, preparation and rescoring work without
them. `examples/openai/replay_without_sdk.py SAVED_SMOKE_ROOT NEW_OUTPUT_ROOT`
verifies that a core-only installed wheel (no OpenAI or HTTPX) can prepare,
render with verified custody and replay byte-identically, while blocking provider
generation imports/network and preserving run bytes, modes and modification times.
Structural policy/result schemas accompany runtime cross-field validators.
Actual provider compatibility, billing, account ownership and live acceptance remain
unverified and gated. Other APIs/models require their own reviewed contracts.
