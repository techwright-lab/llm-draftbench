# Offline contract v1

This is the **implemented** offline slice, not a benchmark protocol or a claim of
model quality. `draftbench validate` is the authoritative validator;
`draftbench inventory` reports aggregate input coverage. Neither executes models.

## Files and identities

- A caller-selected `suite.json` contains `schema_version: "1"`, `suite_id`,
  `purpose`, `rights`, `identity` and one `cases` file reference.
- `cases` points to UTF-8 JSONL. Each nonempty line is a complete v1 case.
  An empty suite and blank lines are rejected. CRLF and a final newline are allowed.
- Every file reference contains a relative `path` and lowercase hex `sha256` of
  **raw file bytes**, including whitespace. References are resolved from the
  manifest directory, never from the case file's directory or current directory.
- Suite and case `identity` are SHA-256 of the root object with only its own
  `identity` key omitted, encoded by `draftbench.identity.canonical_bytes`:
  UTF-8, `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`,
  `allow_nan=False`. No Unicode normalization. Array order is significant.
  This is a versioned Python JSON profile, **not RFC 8785/JCS**.
- Only JSON types and string object keys are accepted; duplicate JSON keys,
  BOMs, non-finite numbers (including overflow), lone surrogates and excessive
  nesting are rejected. Schema v1 uses no floating-point numeric fields.
- Recompute case identity after *any* edit (including labels or unit order),
  then the raw JSONL hash, then suite identity. Hashes detect drift against a
  supplied manifest; they do not authenticate its author or prevent deliberate
  re-sealing. Keep trusted manifests outside an editable import tree if custody
  matters. Drafts, prompts, reviews and labels are covered by the case identity;
  they are not separately content-addressed v1 records.

Example identity construction, before writing canonical artifacts:

```python
from draftbench.identity import canonical_bytes, identity

case["identity"] = identity(case)
case_line = canonical_bytes(case) + b"\n"
```

`case.schema.json` and `suite.schema.json` are Pydantic's JSON Schema exports
(Draft 2020-12 vocabulary), checked against the models by tests. Regenerate with
`Case.model_json_schema()` / `Suite.model_json_schema()` and JSON indentation 2.
Run `uv run --offline python schemas/export.py` to regenerate both exports.
Schemas validate **structure only**: identity, availability/payload relationships,
ID references, split isolation, rights consistency and on-disk integrity also
require `load_suite`. Python's strict integer rejection of `1.0` is stronger than
JSON Schema's mathematical integer semantics. Unknown fields and type coercions
are rejected by the authoritative loader, including boolean integer versions.

## Case data and boundaries

All fields are required; unavailable values are explicit, not silently defaulted.
See the generated schemas for exact enums, cardinalities and nested properties.

- `case_id`, `source_family`, `split`: stable exporter-assigned IDs; split is
  `development` or `confirmation`. Case IDs must be unique in a suite. A source
  family cannot cross splits, even when the source is missing. Available identical
  source bytes cannot cross splits even under different family IDs. Missing
  sources have no hash and never share an invented empty-source hash. This cannot
  detect semantic near-duplicates:
  exporters must assign related variants to the same source family.
- `rights`: license/terms identifier, `private` or `redistributable` usage and
  an explicit authorization statement. Every suite, case and file artifact needs
  rights. Redistributable containers cannot include private children. These are
  exporter assertions, not legal verification or public-release permission.
- `metadata`: required, fixed-shape product-neutral context: `content_type`,
  `domain`, `lane`, `body_format`, `source_system` (exported-from system name), and
  `source_revision` (the source-system revision). Every dimension is an explicit
  bounded text observation, not a free-form dictionary. Known strings are preserved
  exactly; unknown and unavailable dimensions remain null. Metadata is identity-
  covered but excluded from generator projections and aggregate reports. It is
  classification context, not an instruction to the generator. Per-type report
  presentation is not implemented in this slice.
- `source`: required availability wrapper, `{state, artifact}`. Only `available`
  carries one hashed local artifact with rights and producer provenance; every
  other state requires `artifact: null`. Missing original sources are loadable
  without inventing file references or empty source text. Only supplied artifacts
  undergo hash, path, rights and artifact-ID checks. Family split checks always run.
- `generator`: explicit writer/reviewer/revision input availability. Available
  inputs contain ordered messages, prompt version, evidence/draft/review ID
  selections, provenance and capture basis. Provider settings, tools, multimodal
  input and provider-specific invocation schemas are not supported in this slice.
- `evidence`: availability plus hashed local artifacts. No URLs are resolved.
  Documents are checked as opaque bytes, not parsed or executed.
- `history`: availability, draft versions and review rounds. Each draft retains
  an **ordered list of publication units** (`text` or `social_post`); threads are
  never concatenated. Review IDs reference existing draft IDs. Draft versions
  and draft IDs are unique per case; multiple distinct reviews may share a round.
- `evaluator`: labels are segregated from generator inputs. Each label records
  draft ID, question ID/version, answer state/value, producer provenance and an
  independent required `basis`: `historical_exact`, `reconstructed` or `synthetic`.
  Basis describes how the annotation record was captured, not its correctness,
  answer availability, producer kind, or gold status. It is identity-covered.
  A historical label can have an unknown answer; a synthetic label may be known.
  Known answers may be boolean, integer or nonempty text. A false answer is not
  missing; unknown/inapplicable/invalid values must be null.
- `Producer.kind` preserves known human, model, workflow or synthetic origin.
  `name`, `version`, `requested_model`, and `served_model` are individually required
  bounded text observations: `{state, value}`. `known` requires a nonempty string
  of at most 512 characters; `unknown` (not known) and `unavailable` (known to be
  absent from the export) require null. The same observation contract is used for
  metadata. No primitive coercion, extra keys, guessed names, or sentinel strings
  are needed. Model producers may have any mix of known/unknown/unavailable IDs;
  a known requested ID never implies a known served ID. For non-model producers,
  both model fields must instead be `{state: "inapplicable", value: null}`.
  Inapplicable is forbidden for model producers and for name/version/metadata.
  Producer kind itself must be known in v1; an unknown producer kind is not inferred.

`generator_payload(case, role)` is an allowlisted **structured projection**, not
an adapter or executable request: it contains messages, source/evidence file
references and explicitly selected drafts/reviews, but no evaluator labels,
label bases, case metadata or producer metadata. Source projection is
`{state, file}` with a hashed file reference only when available and null otherwise.
Evidence selections still reference supplied artifact IDs: absent evidence is
recorded by its case availability, not dangling fabricated IDs. Empty selections
are not evidence that missing original evidence was unnecessary. The projection
does not materialize files. Exporters still must audit
free-text prompts and sources for answer leakage; structural separation cannot
scrub labels already copied into a prompt. This API is not used to call a model.

## Availability and honest replayability

Source, evidence, history and each role use these distinct states:

| State | Meaning |
| --- | --- |
| `available` | A structurally valid payload is supplied. |
| `unavailable` | Exporter knows the payload is not present. |
| `unknown` | Exporter does not know whether it exists. |
| `inapplicable` | This material or role does not apply. |
| `invalid` | Exporter determined it is unusable; no usable payload supplied. |

Only `available` accepts a payload. `invalid` is a data-availability statement;
it is different from a malformed suite, which fails validation entirely.
Labels use `known`, `unknown`, `inapplicable`, `invalid`, not numeric substitutes
for non-answers.

Available role input bases are reported separately:

- `historical_exact`: the exporter asserts the supplied prompt/context was
  historically captured exactly. Hash verification checks its present integrity,
  **not** the truth/completeness of that assertion or reproducibility of a served
  model. V1 does not reconstruct a complete historical provider request.
- `reconstructed`: assembled later, including current prompts applied to old
  drafts. Never counted as historically exact.
- `synthetic`: intentionally fabricated infrastructure-test input.

`roles` counts describe **role input capture only**, never replay readiness.
They preserve the exporter-asserted basis even when original sources or evidence
are missing; validation does not reclassify historical truth. Every role's category
counts sum to the case count. Source/evidence/history availability, label
answer-state counts (`labels`), and annotation-basis counts (`label_bases`) are
separate. Each label contributes once to each of its independent count axes.

`replay_readiness` separately counts `historical_exact_inputs_ready` and `not_ready`
per role, also summing to case count. The conservative ready category requires an
available historical-exact role packet, an available source and evidence that is
available or explicitly inapplicable. Unknown/unavailable/invalid evidence blocks
it even if evidence selections are empty; any missing source blocks it, including
an inapplicable source. Reconstructed/synthetic packets are never counted here.
This is offline input completeness, **not** actual replay, full provider-request
capture, verified historical provenance, deterministic model reproduction or gold.
Inputs may still lack unsupported historical provider settings.

Reports contain no identifiers,
source text, bodies, file paths or label values. Reports set
`execution_performed: false`; there are no scores or model rankings.
A `synthetic_infrastructure` suite may not claim a confirmation split. For real
private evaluation suites, confirmation is only a declared split: v1 does not
implement hidden-set access control or certify that it stayed unexposed.

## Local loading, limits and output safety

Internal references use a narrow relative POSIX syntax: ASCII letters/digits,
underscores, hyphens and periods within components, with no leading period,
absolute path, drive/URL scheme, backslash, empty component or traversal. The
loader also rejects Windows device names (including names with extensions),
trailing periods and components longer than 255 ASCII bytes. It rejects all
reference symlinks (even in-root ones), directories and special files. Explicit
input/output CLI arguments may be outside the repository. Private
suites require no special naming convention, configuration, credentials or DB.

Limits: 8 MiB per file, 64 MiB total unique input reads, 10,000 cases, JSON depth
48, plus the per-field/list bounds in JSON Schema. Repeated artifact paths are
verified once per invocation; contradictory hashes are rejected. No network
resolver exists. Input trees must remain unchanged during loading: path checks
and hash caching are **not a sandbox against concurrent hostile filesystem
mutation**. Use an immutable snapshot for untrusted exports.

Library callers must also leave validated objects unchanged: Pydantic's frozen
models do not deeply freeze nested lists. `inventory` trusts the loaded objects
and does not revalidate them. If a caller edits nested data, reload and validate
the newly sealed suite before reporting it. The CLI performs no such mutation;
generator projections are detached copies, not mutable views into the case.

CLI stdout is aggregate JSON; validation failures go to stderr as fixed safe
codes, exit status 2, without validation values or tracebacks. Successful commands
exit 0. Default operations do not write. `--output PATH` creates a new report
exclusively, with POSIX mode 0600; an existing path (including a symlink) is never
overwritten. Parent directories are not created. An interrupted/failed write can
leave a partial new report, which is not a successful result. The only report
content is safe aggregate data; no inputs are copied to the output.

## Explicitly not implemented

No Inspect AI dependency/adapter, provider SDKs, network calls, model execution,
general semantic quality judge, annotation UI, publication gate, confirmation custody ledger,
private-system exports or hidden confirmation harness. These require separate
contracts and tests, not placeholder commands. The separate
[synthetic run contract](RUN_CONTRACT.md) defines the implemented fake-adapter
run/resume commands and local attempt ledger; it does not extend suite v1 into a
full provider-request or measurement protocol. The separate
[scoring contract](SCORING_CONTRACT.md) defines artifact-bound deterministic checks
and reference-conditioned reviewer metrics. It does not infer gold labels from
this suite's historical workflow labels or claim universal factuality assessment.
The separate [annotation contract](ANNOTATION_CONTRACT.md) defines private blinded
packet export, immutable original-response imports and separate adjudications;
it does not certify rater identity, independence or hidden confirmation custody.
