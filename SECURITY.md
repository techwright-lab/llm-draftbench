# Security policy

## Reporting

Do not put credentials, customer material or private dataset content into a public issue. Use GitHub's private vulnerability reporting for this repository if enabled. If it is unavailable, contact a repository maintainer privately before sharing sensitive details. No response-time commitment is currently offered.

## Trust boundaries

Suite manifests, source text, candidate outputs and annotations are untrusted data, not executable instructions. A suite must not load arbitrary Python, execute shell commands or retrieve remote references. File references must remain within their suite boundary. Validation must fail explicitly on malformed or inconsistent records.

The tooling performs offline validation, coverage reporting and explicitly synthetic run/resume. Its local SQLite attempt ledger is not a product database connection. It must not import product applications, access their databases or discover their credentials. Private suite files, run stores, labels, native logs and identity mappings belong outside this public repository.

Run/resume requires a private directory outside Git and a process-exclusive lock. Immutable object hashes and append-only ledger transitions detect ordinary corruption and prevent accidental rewriting; they are not authentication or a sandbox against a hostile same-user process. Do not open an untrusted, concurrently writable run directory from a privileged process. Neither the fake adapter nor the provider adapters materialize archival source/evidence files into input text; provider prompts carry only the exported system and user messages, and a role input that names evidence is refused.

Reports can still expose metadata through identifiers and counts. Review all output before external sharing. Structural checks and hashes do not prove that content is safe to disclose, legally reusable or factually correct.

## Explicit pilot credentials and dispatch

Only `pilot run` and `pilot resume` load the file explicitly named by
`--env-file`, after verifying the exact approved plan and tariff attestation.
There is no automatic dotenv discovery or ambient provider-key fallback. Never
shell-source the file. Only the four `DRAFTBENCH_*` names in `.env.example` are
accepted; mode 0600, current ownership, regular-file and no-symlink/no-hard-link
checks fail closed. Shell syntax, expansion, quotes, multiline/duplicate/unknown
fields and dummy/empty values are rejected before SDK dispatch. Never pass a key
on a CLI argument or copy one into a plan. Credential and SDK exception text is
not logged by the launcher.

Preparation/preflight are offline and do not read credentials; saved reporting
and replay do not import credential loaders or generation SDKs. Plan/code/data,
route, pinned schema, tariff and dependency changes invalidate approval. A
TrustGrowth review replay response is validated against the saved lab outputs
and bound into the revision request's custody; it cannot widen the approved
models, caps or spend. Approval is a
local operator attestation, not a cryptographic identity or a defense against
hostile same-user Python. Freeze inputs; protect account access and private output
roots. No test of this launcher proves real provider access or tariff validity.

See [the operator guide](schemas/PILOT_OPERATOR.md). Only human-merge, local
credential replacement and explicit post-merge authorization may precede a real
pilot. Keep every run under its original shared campaign; replacing its database
would evade the budget boundary and is prohibited. Preserve failed/uncertain
artifacts and reservations. There is no automatic retry or publication.

## CI and provider execution

Untrusted pull requests must never run with provider secrets or on privileged local infrastructure. Do not use privileged pull-request workflows to execute fork code. Future API-backed operations require an approved exact revision, lab-owned credentials, data-egress approval and bounded spend. No default inference, paid fallback, telemetry or automatic publication is authorized by installation or testing.
