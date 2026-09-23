# Shared campaign admission v1 (offline foundation)

> Five-model extension: [FIVE_MODEL_CONTRACT.md](FIVE_MODEL_CONTRACT.md) adds
> mandatory campaign enrollment for GPT-6 and Anthropic pilot policies. The
> original foundation scope below describes the legacy GPT-4.1 integration; its
> optional-campaign and no-new-provider statements do not apply to the extension.

`draftbench.campaign.CampaignBudget` is a provider-neutral local admission ledger.
It does not implement a provider, verify published prices, authorize inference,
reconcile a bill, or enable a live CLI mode. Provider names and prices in tests
are synthetic. No additional model identifier or provider is supported by this
change.

## Scope and ceiling

- Explicitly create **one** database with `CampaignBudget.create(path)` for the
  campaign. It has a generated identity, fixed currency **USD**, and immutable
  **US$50.00 total** ceiling across every enrolled run, account and provider—not
  US$50 per run, model, process or provider. Per-run limits remain additional
  constraints, never replacements for this ceiling.
- Reopen that same file with `CampaignBudget.open(path)` for every subsequent
  process/run. Creation is exclusive, opening never creates, and neither API
  resets or increases a ceiling. The trusted host must pass the same authority
  to **all** participating runs; unrelated/unscoped legacy runs are not covered.
- This is a local-file authority, not an account-wide provider spending limit.
  Separate files are separate campaigns; copying, deleting, restoring stale
  snapshots or branching a campaign DB defeats that authority and is forbidden.
  Use a controlled directory and a local filesystem with working SQLite locks,
  atomic file operations and fsync. Do not run this on network/sync filesystems.
- `identity` returns `campaign_id`, `currency`, and `ceiling_usd`. `summary()` adds
  `reservation_count` and `reserved_usd`; these are retained upper-bound
  reservations, **not actual charges**. No aggregate bill/reconciliation claim
  follows from a successful reservation.

## Admission and exact amounts

`reserve(attempt_id=..., run_digest=..., request_digest=..., config_digest=...,
provider=..., currency="USD", upper_bound=Decimal(...))` serializes admission
with SQLite `BEGIN IMMEDIATE`, `synchronous=FULL`, and an append-only reservation
row. The aggregate check and insertion are the same transaction; schema guards
also prohibit updates, deletions, replacement and over-cap insertion.

The host must supply a known, independently verified **per-request maximum**
including all potentially billable work under the exact configuration. Unknown,
non-finite, nonpositive, float/string, non-USD and unsupported-range quotes fail
closed. There are no inferred rates or token estimates here. A quote alone is
not evidence that an unimplemented provider or model can safely be dispatched.

Amounts are stored as integer 10^-14 USD units, rounded **up** using exact integer
ratios, independent of the caller's Decimal precision. Quotes may have at most
100 coefficient digits and exponent -100 through 2, and cannot exceed the
campaign ceiling. Replays bind the exact numerical quote, not just its rounded
storage amount. Repeating an identical attempt is idempotent, including after
capacity exhaustion; changing its request, configuration, run, provider or
amount fails closed. Attempt IDs are 32 lowercase hexadecimal characters;
digests are 64 lowercase hexadecimal characters. Provider keys use the ledger's
bounded ASCII identifier profile. The configuration digest must include the
exact model, account route, pricing, caps and other dispatch configuration.

## Existing workflow integration

The existing `run_openai`, `resume_openai` and `approval_scope` embedding APIs
accept an optional `campaign=budget` object. This adds no CLI switch. New scoped
runs freeze the campaign identity in the manifest and trusted approval binding.
Resuming a scoped run without that campaign, substituting another campaign, or
silently enrolling an existing unscoped run is refused. Existing model
allowlists, provider wire contracts, approval callbacks and credential handling
are unchanged.

The run lock serializes each run. A durable local reservation supplies the
attempt UUID; the shared campaign reservation then commits **before** the local
in-flight/start transition and any dispatch. The campaign row binds the local
attempt, request, exact policy digest and manifest/run-directory digest. These
are two databases, intentionally not presented as a distributed transaction:

| Interruption | Recovery |
| --- | --- |
| Before local reservation | No dispatch, no reservation |
| After local reservation, before shared admission | No dispatch; resume tries shared admission with the same UUID |
| During shared transaction | SQLite rolls back an uncommitted transaction or retains a committed one; no dispatch before acknowledgement |
| Shared commit acknowledged or commit outcome ambiguous | Never release capacity; reopen and idempotently check the same binding |
| Shared reservation committed, before local start | Reservation retained; resume may start the same local reserved attempt |
| Local start committed, before/during/after dispatch | Existing recovery marks in-flight work uncertain; no implicit retry or refund |
| Result saved/completed, provider failure, malformed/unknown usage | Full original reservation retained forever |

A denied shared admission can leave one local attempt in `reserved` state.
It has not dispatched; the existing per-run report's `reserved_cost` includes
that local reservation. Only the campaign `summary()` describes **shared**
capacity. Do not sum or equate those two accounting surfaces. This conservative
stranding is preferable to refunding an ambiguous commit or creating an
unreserved dispatch gap. There is deliberately no settlement/refund/reset API.

## Reporting and verification

Saved-run reporting remains read-only, SDK-free and independent of the campaign
file: it verifies existing artifact custody, not current campaign solvency.
Reports do not open, recover, mutate or automatically reconstruct a campaign.
The embedding host inspects campaign summary separately when needed.

Offline tests cover two synthetic provider keys, shared multi-run exhaustion,
real competing spawned processes, exact idempotency/binding failures, wrong
currency and unknown amounts, immutable ceilings, process death after commit,
uncommitted journal recovery, both database boundaries, retained uncertain
attempts, approval/resume binding and reporting without the campaign file.
No live inference or new provider implementation is part of this contract.
