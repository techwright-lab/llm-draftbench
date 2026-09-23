# Blinded annotation contract v1

This file-first workflow exports private annotation assignments, imports original
individual responses without replacing them, and records adjudications separately.
It does not authenticate a human, certify independence, create gold by majority
vote, call a model or publish results. Blinding is a metadata projection, **not**
an independently protected confirmation-data boundary.

## Operator workflow

```sh
uv run --offline draftbench review export examples/scoring/scoring.json --rubric examples/annotation/rubric.json --raters examples/annotation/raters.json --output /path/to/new-session
uv run --offline draftbench review import /path/to/new-session /path/to/returned-responses.json
uv run --offline draftbench review status /path/to/new-session
uv run --offline draftbench review records /path/to/new-session --output /path/to/private-records.json
uv run --offline draftbench review adjudicate /path/to/new-session /path/to/adjudicated-responses.json
```

The session directory must be new, private and outside Git. Parent directories
must already exist. POSIX locks, exclusive creation and the existing immutable
artifact store protect ordinary concurrent use. Files are 0600; directories 0700.
No existing output is replaced. A failed initialization may leave a private
incomplete directory, which is not a successful export and is never silently reused.

CLI output contains aggregate counts only, never rater names, task/source IDs,
paths, artifact text or responses. Exit 0 means the requested operation completed;
it does not mean all assignments are answered or that the labels are correct.
Errors return exit 2 and fixed safe codes.

## Custody and sharing

A session contains:

- `session.json`: private session/manifest pointer.
- `custody.json`: private readable mapping from your roster keys to packet files;
  consult this locally when assigning packets, never send it to raters.
- `objects/`: immutable, hashed snapshots of the original scoring bundle, neutral
  rubric, private roster, assignment mappings, blind packets and blank templates.
- `annotations.sqlite3`: append-only original/adjudication records.
- `run.lock`: a reused process-exclusive private-session lock, not model execution.
- `packets/<opaque-rater-id>.json`: one immutable blind packet for that assignee.
- `packets/<opaque-rater-id>.responses.json`: its editable response template.

**Give a rater only their packet and response template. Never share the whole
session, object store, database, roster or custodian record export.** Packets are
private review handoffs, not automatically redistributable public artifacts.
Custodian record exports are forbidden inside the current blind-packet directory.

Real rater keys/producer identity and mappings to original case, source-family,
unit/source/criterion IDs stay private. Every rater/task gets fresh opaque aliases;
packet task order is independently randomized once and frozen. Within-artifact
publication-unit order and exact text are preserved, never shuffled or concatenated.

The packet deliberately excludes prior reviewer decisions, scores, critiques,
reference labels/defects, alignments, producer/model metadata and original private
IDs/rubric names. The exporter uses an allowlist, not a recursive source dump.
Only explicitly supplied inline scoring sources are displayed; opaque product
export sidecars may contain labels and are never displayed automatically.

**Content itself is not anonymized.** An artifact, source or operator-written
instruction may name its author/product or reveal an answer. The custodian must
review these before sharing. Hashes and aliases do not scrub free text or prove
that the rater remained blind. Separate OS/storage custody is required before
claiming an untouched confirmation set.

## Neutral rubric and frozen versions

Export requires a separately supplied `AnnotationRubric` with matching original
rubric ID/version, neutral instructions, and question text for the source cases'
check IDs. The same mandate may cover a superset of case-specific criteria; only
each case's actual criteria are shown. This is deliberate: the scoring rubric can
contain prior expected values, observed spans and relevance judgments, which would
bias initial independent labeling.

Display criteria retain legitimate applicability and length/literal constraints.
Fact/citation criteria do **not** expose their prior observed/expected values,
locations, relevance decisions or source-linking annotations. Necessary neutral
brief/context must be supplied through the mandate or inline source packet, not
inferred from private prior evaluations. Missing context remains a reason to
abstain; export mechanics do not validate annotation-instrument quality.

Task identity binds the blinded ordered artifact, supplied sources, displayed
questions, constraints and presentation position. Response bindings include
session/packet/rater alias, frozen packet identity, complete task presentation
order, task identity, blinded artifact digest and displayed rubric digest. The
private mapping binds these to the original case identity, artifact hash and
rubric version. Altered packet files or mismatching returned bindings are rejected.

Imports attach only to the frozen versions. Editing an original source file later
does not make old labels apply to the new draft: create a new session for changed
inputs. No automatic projection of these records onto a newer scoring bundle is
implemented; such a consumer must compare exact version/context bindings.

## Answering

Use a JSON viewer for the immutable packet. Edit or copy the response template;
it may be reformatted freely. Do not edit the packet or any binding fields.
Returned response batches are intentionally not self-sealed: humans can change
answers without recomputing a checksum. The importer seals the captured record.

Each response identifies its task and retains:

- A stable response ID, timestamp with timezone, and explicit answered/unanswered
  status. `rated_at` is the rater's declared time; the store independently records
  `received_at` when the record is first inserted.
- Explicit acceptable/unacceptable/abstain decision.
- Criterion states, including unknown/inapplicable/invalid where appropriate.
- Complete/partial/unavailable defect-inventory declaration and defect spans.
- Notes, preserved as inert text.

An unanswered template has no timestamp/decision, labels, defects or notes. It
creates **no annotation row**. Missing rows and unanswered templates remain pending.
Answered partial/abstaining records are preserved with explicit missing/unresolved
criteria and incomplete coverage; no missing answer defaults to a pass. Completion
also requires an explicitly complete defect inventory. Partial/unavailable
inventories cannot complete an assignment or resolve a disagreement.

Defect spans use exact Unicode-codepoint offsets and quote text in blinded units.
The importer validates them, then preserves the raw response while translating
aliases back to original unit and criterion IDs in the private normalized record.
A span from another artifact, unknown criterion or wrong presentation identity
rejects the batch; no partial earlier rows are left behind.

Producer kind, name, model metadata and declared independence come from the frozen
private roster, not editable response fields. A file does not prove who filled it
in. Synthetic source sessions remain explicitly synthetic in normalized capture
basis; real-session capture is historical-exact recording, not correctness proof.
No importer turns a model-produced or workflow-produced rater into a human.

## Originals, duplicates and disagreements

Imports are atomic and append-only. Re-importing an identical record ID/payload is
idempotent and preserves its original receipt timestamp. Reusing an ID for changed
content is a conflict and rolls back the entire batch. A distinct response ID can
record another opinion or correction on the same assignment; it does not replace
or silently supersede an earlier one.

Status distinguishes record counts, received assignments, complete/incomplete
coverage, pending assignments and assignments with multiple originals. Known
acceptable/unacceptable disagreement is reported at case level. These counts are
not rater-reliability estimates, independent sample sizes or a chosen gold label.

## Separate adjudication

Adjudications use a distinct response format and `review adjudicate` command.
They retain the same packet/task/version bindings and add `based_on` original
record IDs. Every referenced record must exist, be an original annotation, and
concern the exact same frozen case and annotation mandate. A new adjudication must
account for all currently recorded original opinions for that case, not cherry-pick
favorable ones.

The store retains both originals and adjudications. It never averages, overwrites,
or automatically promotes an adjudication into a scoring reference. A later
original opinion makes an earlier resolution non-current for status purposes;
that earlier adjudication remains intact. Re-importing the identical old
adjudication remains idempotent, not a new current resolution. A complete new
adjudication can cover the expanded original set explicitly.

An initial rater may also adjudicate; their identity and dependencies remain
visible to the custodian. This is not proof of a second independent editor. The
first real pilot may remain a named-single-editor development study.

## Limits and integrity

Export is bounded to 256 source cases and 16 raters, 8 MiB per artifact/file, and
64 MiB for the generated export. Oversized work fails explicitly; it is not
silently truncated. Imports use the existing bounded local reader. The annotation
store caps a batch at 4,096 records, each at 8 MiB, total retained canonical record
bytes at 64 MiB, and rows at 100,000. Private record exports are also bounded.

The SQLite store verifies its schema, metadata and record hashes, uses durable
transactions, and prevents ordinary UPDATE/DELETE/replacement of stored history.
These controls do not sandbox a hostile database owner able to disable triggers,
replace directories or reseal every artifact. Custodian files and ancestors must
remain controlled. Raw private record exports contain identities and labels and
must never be treated as blind packets or public release artifacts.

Pairwise-preference tasks, semantic disagreement matching, rater authentication,
annotation UI, automatic scoring-reference promotion, hidden confirmation custody
and live provider execution remain outside this slice.
