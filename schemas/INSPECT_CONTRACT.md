# Offline Inspect SDK contract

This is an **offline infrastructure contract**; Inspect has no live execution
path. `inspect-fixture` uses the installed `inspect-ai==0.3.223` SDK and an
in-process `ModelAPI` fixture transport. There is no HTTP client, provider factory,
credential discovery, subscription access, fallback route or live-enable flag in
this adapter. The literal requested and served fixture model is
`draftbench-fixture`; it is not a measured LLM.

## Reproduce

```sh
uv sync --locked --extra inspect
uv run --offline --extra inspect pytest tests/test_inspect_adapter.py
uv run --offline --extra inspect python examples/inspect/smoke.py examples/smoke/suite.json /path/to/new-private-experiment
```

The last command runs the actual CLI entry function through pause, resume,
mechanical score preparation, scoring, report rendering and exact report replay.
It blocks network socket creation, DNS and connect calls; anonymous local
AF_UNIX socketpair descriptors remain available solely for asyncio wakeups.
It prints synthetic status, completed attempt count and replay equality.
Dependency installation can use the package index; the experiment cannot.

Alternatively use the normal CLI:

```sh
draftbench run examples/smoke/suite.json --adapter inspect-fixture --output /path/to/new-run --inspect-policy /path/to/policy.json
draftbench resume /path/to/new-run
draftbench report prepare /path/to/new-run --output /path/to/new-prepared
```

Example policy (all fields are optional; unknown keys and model routes fail):

```json
{
  "model": "draftbench-fixture",
  "max_requests": 3,
  "max_input_bytes": 100000,
  "max_output_tokens": 4096,
  "max_total_tokens": 312288,
  "timeout_seconds": 30,
  "max_cost_usd": 0,
  "price_per_million": 0
}
```

Zero fixture pricing is an explicit local-fixture admission assumption, **not**
observed provider usage or a promise of free live calls. A spend-bound policy with
unknown pricing is refused. Nonzero prices are useful for offline cap tests only.

## Execution and admission

* Registered writer, reviewer and revision `Task` factories bind the local
  fixture model, one epoch and the SDK's single-generation solver with tool
  calling disabled. The adapter invokes that solver with a single-use generate
  callback through the real SDK `Model.generate`; it does not invoke Inspect's
  top-level eval runner, retry runner or provider/environment model selection.
* Draftbench owns the append-only plan and lifecycle. The exact saved writer
  result is bound into review; both saved draft and critique are bound into
  revision. No alternative served configuration or automatic fallback exists.
* Runs are **serial** (concurrency 1). `max_connections=1`, `max_retries=0`,
  `num_choices=1`, `best_of=1`, cache and batch disabled; no rerolls or tool loop.
  Tests force the SDK retry predicate true and still observe just one transport
  attempt. SDK version changes require re-verification, not silent acceptance.
* Run policy is frozen in the content-addressed manifest and every request.
  `Ledger.reserve` checks the run's cumulative request, conservative token-unit
  and cost ceilings inside the same `BEGIN IMMEDIATE` transaction that durably
  appends the reservation. The whole-run POSIX lock separately prevents competing
  dispatchers. Independent-connection reservation races are tested.
* Every reservation consumes `max_input_bytes + max_output_tokens` admission
  units. The transport caps completion UTF-8 bytes conservatively at the output
  token limit; these are **not measured tokens** and this accounting must not be
  reused as a provider tokenizer. Input size is bounded before SDK dispatch.
  SDK total/attempt timeouts and an outer asyncio deadline bound calls.
* Reservations are never refunded, including known failures, timeout, crash and
  missing usage. In-flight crash recovery remains uncertain and never retries.
  Uncertain charges remain unknown rather than zero. Admission exhaustion leaves
  unattempted work planned and returns incomplete status; resume cannot increase
  a frozen cap. Use an explicitly separate run for a changed configuration.

## Frozen evidence, failures and reports

Successful results retain the exact native SDK `ModelOutput`, requested/served
fixture model, full effective generation configuration, native input envelope,
prompt digest, parent digests, SDK and selected execution dependency versions,
observed local latency, nullable native usage and nullable provider latency/cost.
Synthetic local elapsed time is not provider performance. Inspect's SDK-populated
`ModelOutput.time` remains unchanged in `native_output`; `latency_seconds` records
local adapter elapsed time. `provider_latency_seconds` is always null for the
fixture, including native failure outcomes; non-null values in successful fixture
results are rejected by runtime validation. The exact native
completion is parsed once; validation checks its binding to the normalized units.
Reporting and rescoring never import the SDK or invoke a generation callback.

Native errors and stop reasons (`max_tokens`, `model_length`, `content_filter`,
unknown, etc.) are not converted into success. Failed/limited attempts retain an
exclusive fsynced `attempt-<id>.json` receipt binding the request and an immutable
native outcome object. Reports verify and freeze these private receipts. If a
process dies before the terminal ledger event, any receipt is orphan evidence,
not completion; recovery remains uncertain. Exception text is not echoed in the
CLI; exception type and unknown charge are recorded instead. A returned result
that fails request binding (role, request digest or parent digests) is an
observed failure: it is recorded as `failed` with `invalid_adapter_output` and
a receipt, the same as the `fake` adapter, not as uncertain. Other unexpected
errors outside native normalization conservatively become uncertain.

The existing read-only SQLite/crash-refusal, content-addressed store, private
permissions, append-only attempts, mechanical-only scoring, synthetic watermark,
rights checks and explicit local-release boundary remain in force. New structural
schemas are `inspect-policy`, `inspect-result` and `run-manifest`; runtime validators
add cross-field and exact native-completion bindings.

## Remaining live acceptance

No provider request, inference, account/subscription access, private-data egress,
production change, publication or measured model evaluation has been performed.
Before a live adapter can exist, separately approve the exact model and access
route, authorized inputs/egress, account, pricing source and budget. Implement and
verify provider-specific retry disabling, token/reasoning/cached-token accounting,
served identity and request IDs, uncertain billing, cancellation semantics and
provider-side caps. Local admission is not a universal billing guarantee.
Live outputs require a separate non-synthetic contract/report path; they must
never be forced through this fixture executor or relabeled synthetic. The
current registry deliberately rejects every real model route.
