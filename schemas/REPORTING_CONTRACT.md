# Offline report and local release contract (v1)

This is an exploratory development artifact, not a leaderboard, semantic judge,
production publication gate or hosting service. Synthetic fake/Inspect runs and
verified saved OpenAI preparations have distinct custody/provenance contracts
(see `OPENAI_CONTRACT.md`); reporting never dispatches any configuration. **SYNTHETIC — NOT MODEL MEASUREMENTS** remains visible on synthetic
private and public artifacts.

## Preparation and read-only binding

`report prepare RUN --output NEW_DIRECTORY` verifies the frozen run manifest,
plan, request and result hashes under the run lock. Only ledger `completed`
writer/revision outputs become scoring cases. Unit order, IDs and content remain
exact; the scoring case ID is the work ID, with the original source-family ID.
The rubric is `mechanical-only` v1, one required `nonempty` check. It does **not**
assert factuality, relevance, editorial suitability or independent gold.

The private preparation folder contains:

- `bundle.json`: existing strict `draftbench-scoring-v1` contract, initially unknown
  references and no structured reviewer or finding alignment.
- `rubric.json`: neutral annotation mandate for that exact mechanical condition.
- `bindings.json`: `format: draftbench-run-bindings-v1`, `run_snapshot_digest`,
  `bundle_identity`, and `cases` mapping scoring case IDs to `work_id`, original
  `case_identity`, `request_digest`, `result_digest`, `role`, `artifact_sha256`,
  and `scoring_case_identity`.

`report render --run RUN --bundle BUNDLE --bindings BINDINGS` requires exact
preparation equality. Do not mutate a prepared bundle to attach labels; use the
explicit session/selection path instead. A changed run requires fresh preparation.
Provider preparations use `--provider-run RUN --bundle BUNDLE --bindings SIDECAR`
with the OpenAI sidecar, never `--run`. Supplied bindings without both a run and
bundle fail closed; they are never ignored. Standalone bundles are explicitly
labeled `custody=standalone`, not verified run evidence.
The status-only `--run RUN` form needs no completed output. The scoring-only
`--bundle BUNDLE` form marks execution `not_run`. No reporting command invokes an
adapter, repairs ledger states, recovers, dispatches, or fetches original source
files. Pending/failed/limited/uncertain/result-saved work remains in operational
counts. A result saved without completion is never promoted to ready evidence.
Plain critique feedback is evidence, not a fabricated semantic verdict.

Run and annotation databases are opened in SQLite read-only mode while their
existing cooperative locks remain held. Reporting refuses any `-journal`, `-wal`
or `-shm` sidecar (including empty/cold ones), and WAL-format databases even
without sidecars, rather than recovering or checkpointing them. Safe refusal
codes are `ledger_requires_recovery` and `annotation_store_requires_recovery`;
unsafe paths and corrupt headers retain their own safe errors. Refusal preserves
input bytes, names, permissions and modification times. Do not delete sidecars
to bypass this check: recovery belongs in a separate explicit writable operation.
Normal run/resume and annotation imports remain writable. Database files may be
owner-read-only (0400); existing lock files must remain accessible for locking.
The owner must keep input directories stable; hostile same-user replacement is
outside this cooperative-lock threat model.

## Explicit annotation selection

Obtain private record IDs via `review records`. Pass this file alongside
`--session SESSION --selection FILE`:

```json
{
  "format": "draftbench-annotation-selection-v1",
  "records": {"scoring-case-id": "explicit-original-or-adjudication-record-id"}
}
```

Unknown selectors fail closed. Missing selections leave references unchanged.
Selection verifies the entire original scoring case against the annotation
session's frozen source, including artifact, source context, rubric and identity.
The selected raw answer is parsed and re-normalized against its frozen blinded
packet, mandate and roster; normalized fields must match the immutable stored
record. Adjudications must reference exactly all current originals for the case;
later originals make an old adjudication stale. Original selection is explicitly
rater-specific, not majority voting, latest-wins, authenticated human gold or
public-release permission. Operator reports disclose original counts and label
variation. Reference producer kind, basis and independence are preserved.
Partial, abstaining or incomplete selected records keep acceptability unknown.
Old finding alignment is always removed when a reference is replaced.

## Normalized private artifacts

`draftbench-report-v1` contains `schema_version`, content-derived `identity`,
`purpose`, `custody`, `provenance`, `model_execution_performed`, `watermark`,
`interpretation`, `execution_status`, role `panels`,
`scoring_only`, `annotation_choices`, `annotation_coverage`, `reference_conditions`,
`evidence`, and a private `snapshot` used for exact replay. The snapshot includes
original and selected scoring bundles, verified run state, explicit bindings and
private selected record identifiers; do not distribute it.

Each writer/reviewer/revision panel carries configuration, status, metric version,
operation counts, scoring coverage numerator/denominator, source-family count,
scoring cohorts where applicable and explicit unassessed quality/revision-gain/
cost/latency. Operational failures are not content defects. Reference-conditioned
metrics retain the scoring contract's versions, denominators and cohort separation.
Completed mechanical observations remain `inconclusive` for semantic quality.
No costs, latency, revision gain, critic accuracy or zero spend are invented.

Every output folder is new/exclusive, outside Git, mode 0700; every file is 0600.
Files are `report.json`, `report.csv`, `index.html`, numbered local evidence JSON
and `checksums.json`. Checksums cover every emitted file other than the checksum
manifest itself. The latter carries the report identity and exact filename/hash
map. Reading rejects drift, extra files, unsafe paths and mismatched regenerated
outputs. `report replay OLD --output NEW` verifies hashes and frozen bindings and
regenerates the same bytes without loading a run or annotation session. There is
no timestamp noise. Hashes protect integrity, **not origin authentication**: a
party able to replace every file can create a different self-consistent report.

CSV is a deterministic field/value flattening of the visible projection; all
HTML text is escaped, only generated numbered relative evidence hrefs are used,
no scripts or external resources are included, and CSP denies script execution.
Potential spreadsheet formulas (including whitespace/control prefixes) are quoted
as text. Private snapshot data stays in JSON, not embedded executable HTML.
Bounded output: 8 MiB/file, 64 MiB/folder; oversized reports fail rather than silently
truncate. Keep input trees immutable during reading (same local trust boundary as
suite/run/annotation input readers).

## Explicit new local public folder

Release has no default selection. An explicit empty list permits no case evidence:

```json
{
  "format": "draftbench-release-selection-v1",
  "panels": ["writer", "reviewer", "revision"],
  "evidence_ids": []
}
```

`release check REPORT --selection FILE` verifies the exact private report, returns
its `source_report_identity`, the exact `projection_digest` and full public
projection. This preview does not create a public artifact or authorize anything.
Only fixed role-panel fields are supported; v1 intentionally excludes nested
reference-conditioned scoring from public exports. Raw evidence export is currently
limited to verified run-based evidence; scoring-only supplied artifacts remain
private because they do not carry the component-level run rights chain. IDs, producer names, URLs,
prompt bodies, private mappings, native artifacts and annotation notes are absent
from aggregate-only projections. Unit text and rights statements are present only
when their evidence ID was explicitly selected; selection does not anonymize text.

A local export requires an operator-created approval (see generated JSON Schema):

```json
{
  "format": "draftbench-release-approval-v1",
  "approved": true,
  "source_report_identity": "<exact identity returned by check>",
  "projection_digest": "<exact digest returned by check>",
  "reviewer_kind": "human",
  "declared_reviewer": "Operator declaration, kept private",
  "rights_attestation": "Operator-reviewed permission for this exact projection",
  "selection": {
    "format": "draftbench-release-selection-v1",
    "panels": ["writer"],
    "evidence_ids": []
  }
}
```

`reviewer_kind: synthetic` is permitted only for synthetic reports, for test
fixtures; the smoke builder never generates real human approval. Neither kind is
authenticated person proof. Even aggregate counts require explicit digest-bound
approval. Selected case evidence additionally requires every captured case/component
and suite rights usage to be `redistributable`. Missing or private rights fail
closed; Apache code licensing confers no data rights. Verified rights statements
are retained for selected evidence. The operator must review their text for private
information before attesting to the exact projection.

`release export REPORT --approval FILE --output NEW` regenerates the exact approved
projection into a separate new local folder. Public JSON, CSV, HTML and evidence
come from that same projection. Nothing copies a source tree, custody database,
raw trace, identity map, annotation note or approval file. No network, hosting,
publishing, credential use or automatic deployment occurs. The public artifact
contains no approval reviewer identity or private source-report identity. The
private approval remains the custody link between source and projection.
