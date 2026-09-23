# Five-model text provider contract v1 — offline tested

This extends the legacy GPT-4.1 contract; it does not replace it. The exact pilot
IDs are `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `claude-opus-5-5`, and
`claude-sonnet-5`. No aliases, automatic model substitution or fallback exist.
Mocked SDK HTTP success is **not evidence of live model availability or provider
compatibility**. No credentials or live inference are needed for this contract.

## Endpoints and pinned SDKs

- OpenAI `openai==2.29.0`, `httpx==0.28.1`: one non-streaming
  `POST https://api.openai.com/v1/chat/completions`; `n=1`, `store=false`,
  `service_tier=default`. Explicit `reasoning_effort`: Astra low/medium/high/xhigh/max;
  Sol/Luna also permit none. No sampling parameters, logprobs, tools, caching
  directives, batch jobs, fast processing, retries or rerolls.
- Anthropic `anthropic==0.84.0`, `httpx==0.28.1`: one non-streaming
  `POST https://api.anthropic.com/v1/messages`, `anthropic-version=2023-06-01`,
  `service_tier=standard_only`, `inference_geo=global` (not an ambient workspace
  regional default). Explicit `thinking.type=adaptive` and
  `output_config.effort`; Sonnet also supports disabled thinking. Opus cannot
  disable thinking. No manual thinking budgets, sampling parameters, assistant
  prefills, tools, loops, cache directives or batching. Display is omitted.
- SDK constructors and signatures were inspected before use. The newer Anthropic
  1.8.0 client uses a different HTTP stack and ambient custom-header discovery;
  it is deliberately **not** the supported dependency here. No SDK shim is used.
- Both HTTP clients disable environment proxy discovery, redirects and SDK retry.
  Credentials are explicit constructor arguments, including an empty Anthropic
  auth token to prevent ambient-token discovery. Fixture tokens are synthetic.

`max_output_tokens` maps to OpenAI `max_completion_tokens` or Anthropic
`max_tokens`, and caps **thinking plus visible output**, not just answer text.
The pilot deliberately caps this allowance at 32,768 and prompt UTF-8 at 100,000
bytes. This is a supported subset, not a claim about the providers' maximums.

## Frozen pricing provenance, not live tariff verification

Policy `pricing_provenance=public-docs-2026-09-23-v1` freezes the following standard
global USD per-million token rates. Model, rates, provenance, conservative quote,
caps and account route participate in the manifest and approval digest.

| Model | Input | Cached input | Cache write | Output |
| --- | ---: | ---: | ---: | ---: |
| gpt-6-astra | 10 | 1 | 12.5 | 50 |
| gpt-6-sol | 2 | 0.2 | 2.5 | 10 |
| gpt-6-luna | 0.1 | 0.01 | 0.125 | 0.5 |
| claude-opus-5-5 | 4 | 0.2 | 5 (5m), 8 (1h) | 20 |
| claude-sonnet-5 | 2 | 0.2 | 2.5 (5m), 4 (1h) | 10 |

OpenAI input above 272K costs 2x input/cache and 1.5x output for the whole call.
Fast mode, regional premiums, alternative routes and account-specific pricing
are outside this contract. Anthropic prices were recovered from **archives**;
live documentation fetches were blocked. Neither provider's tariff is asserted
to be currently verified for live dispatch.

Source ledger (public URLs, inspected 2026-09-23):

- [OpenAI latest-model migration rules](https://developers.openai.com/api/docs/guides/latest-model)
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
- [OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning)
- [Astra](https://developers.openai.com/api/docs/models/gpt-6-astra),
  [Sol](https://developers.openai.com/api/docs/models/gpt-6-sol),
  [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [Opus overview archive, 20260922181127](https://web.archive.org/web/20260922181127/https://platform.claude.com/docs/en/models/opus-5-5/overview)
- [Sonnet overview archive, 20260922181157](https://web.archive.org/web/20260922181157/https://platform.claude.com/docs/en/models/sonnet-5/overview)
- [Anthropic pricing archive, 20260923181040](https://web.archive.org/web/20260923181040/https://platform.claude.com/docs/en/about-claude/pricing)
- [Messages archive, 20260920181441](https://web.archive.org/web/20260920181441/https://platform.claude.com/docs/en/api/messages)
- [Thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)
  and [prompt caching archive](https://web.archive.org/web/20260922182517/https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

Live host dispatch additionally requires a non-null `verified_tariff_digest`
identifying the trusted host's independent review of this exact frozen tariff,
route and quote, plus the existing exact-scope approval callback and explicit
credential. The digest alone grants no authority; suite data cannot authorize
inference. The separate operator launcher compares an explicitly entered reviewed
plan digest, then supplies bound child approval callbacks. A changed tariff requires a new contract version,
not silently substituting prices. Live validation remains a separate gate.

## Mandatory shared US$50 authority

All five pilot models require the same explicitly supplied `CampaignBudget` on
run and resume, **including fixtures**. Never create one campaign per model or
provider for the real pilot. See [shared admission](campaign-budget-v1.md).
Fixture smoke uses its own synthetic campaign, never the live pilot's authority.
Legacy GPT-4.1 unscoped runs remain backward compatible.

`run_provider` / `resume_provider` reuse the existing durable workflow; shared
reservation commits before local in-flight and dispatch. Provider name, attempt,
request, exact policy and manifest/run binding are frozen. Missing or mismatched
campaigns fail closed. There is no refund, reset or uncertainty retry.

Admission deliberately avoids guessing a tokenizer or treating a byte ceiling
as a proven token limit:

- GPT-6 reserves the full documented 922,000 maximum input at **2.5x** base input
  (long-context cache-write upper rate), plus **1.5x** the output allowance.
  Observed input above 272K is unsupported and produces unknown cost, not a false
  short-context estimate. No long-context request configuration is exposed.
- Anthropic reserves the full 1,000,000 context at **2x** base input (maximum
  1h cache-write rate), plus the output allowance.
- All monetary arithmetic, including scaled reservations, is exact and independent
  of ambient Decimal precision/traps. Usage never releases capacity.

These conservative reservations may stop a run before every role is complete.
For example, Astra at a 100-token output cap cannot complete three requests within
US$50 under this bound. An incomplete cap-limited report is intentional; do not
claim that every five-model comparison fits the budget. Improving bound tightness
requires separately verified input accounting, not lowering a guessed quote.

## Native evidence and read-only custody

Native JSON is saved privately before SDK coercion. IDs, exact served model,
stop reason and nullable usage are retained; served aliases are rejected.
Malformed counters (including booleans, floats, strings and unknown nonzero detail
fields) produce uncertain status and **unknown cost**, retaining the reservation.
Missing usage remains unknown. HTTP errors retain safe request ID/status/code,
never provider error text, credentials or arbitrary headers. Local wall time is
not provider latency; native provider latency remains unknown.

Anthropic output is the concatenation of text blocks only. Thinking and redacted
thinking remain private opaque native evidence; they are not visible answers,
parent prompts or score text. `output_tokens` already includes thinking; the
`thinking_tokens` breakdown is never added again. Cache read, cache creation and
base input are separate; cache-creation children are never double-counted.
Cost estimates are conservative (no cache-read discount; Anthropic writes at
1h rate), not reconciled bills or measured fixture charges.

`prepare_provider` / `report_provider` are SDK-free aliases of the existing
read-only reporting entry points. `report render --provider-run ... --bundle ...
--bindings ...` revalidates exact frozen custody for both providers. Reports label
fixtures synthetic, and never claim actual model measurements. Read-only replay
needs neither the campaign database nor SDKs nor dispatch imports.

## Offline CLI and smoke

Install extras `draftbench[openai,anthropic]`. Create one campaign explicitly
through `CampaignBudget.create(path)` and reuse its path for all fixture commands:

```
draftbench openai fixture-run suite.json --policy astra.json --output astra-run --campaign campaign.sqlite3 --max-steps 1
draftbench openai fixture-resume astra-run --campaign campaign.sqlite3
draftbench anthropic fixture-run suite.json --policy opus.json --output opus-run --campaign campaign.sqlite3 --max-steps 1
draftbench anthropic fixture-resume opus-run --campaign campaign.sqlite3
draftbench anthropic prepare opus-run --rights rights.json --output prepared
```

For explicit three-model live operation, see [the operator guide](PILOT_OPERATOR.md);
preparation remains offline and dispatch requires post-merge operator approval.
`examples/providers/smoke.py` exercises all five IDs with
real pinned SDK MockTransport, one shared budget, pause/resume, preparation,
report rendering and byte-identical replay while socket functions are denied.
`examples/providers/replay_without_sdk.py` repeats saved-run preparation/render/
replay from a core-only wheel with SDKs absent and generation imports forbidden.
