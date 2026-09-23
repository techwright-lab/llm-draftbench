# Synthetic run contract v1

This is an infrastructure execution contract, **not** a measured benchmark. The
only adapter is `fake`, an explicitly deterministic fixture that never calls a
model, browses, evaluates factual accuracy or publishes anything. It supports a
fixed writer → reviewer → revision chain, not the full experimental design.

## Commands and scope

```sh
uv run --offline draftbench run examples/smoke/suite.json --adapter fake --output /path/to/new-run
uv run --offline draftbench resume /path/to/new-run
```

Choose a new output directory outside any Git checkout. The parent directory must
exist. Run/resume currently require POSIX advisory file locking; Windows execution
is not implemented. Offline suite validation and inventory remain separate.

`run` requires an explicit `--adapter fake`; no implicit adapter or provider
fallback exists. It accepts only suites declared `synthetic_infrastructure`, with
development cases and synthetic available role inputs. Real evaluation suites
remain loadable for validate/inventory but are refused by this executor.

`--max-steps N` bounds new synthetic invocations in that command (0–30,000).
Initialization persists every planned work item before any dispatch. Stopping
leaves remaining work planned; `resume` uses only the frozen local run and does
not need the original input directory.

CLI stdout is an aggregate JSON execution summary. It contains complete work and
attempt counts, every state denominator, `model_execution_performed: false`, and
`synthetic_execution_performed`: true if at least one invocation is known to have
occurred, null if none is known but an in-flight/uncertain attempt may have run,
and false if no dispatch was recorded. A recorded dispatch start alone does not
prove that the adapter was invoked. No prompts, case IDs, bodies, labels
or paths are printed. Exit codes: 0 means every planned item completed, 3 means a
valid but incomplete run (including intentionally bounded runs, unavailable,
blocked, failed, limited or uncertain work), and 2 is an invalid operation/state.
Errors use safe codes, not private values or tracebacks.

## Frozen files

- `run.json` binds the run format and manifest digest.
- `objects/<sha256>` stores immutable raw bytes. The same exact bytes reuse the
  same lowercase SHA-256 name; JSON is canonicalized with the existing v1 profile.
- The frozen manifest retains validated suite/case records, the adapter version
  and original file references. Original JSONL/source/evidence bytes are copied
  as verified objects. Resume checks all declared input hashes, case identities,
  the exact deterministic work plan, requests and known results.
- `ledger.sqlite3` persists immutable plan, attempts and state-transition history.
- `run.lock` serializes execution/resume. A concurrent resumer fails instead of
  marking another live process's work uncertain.

Directories are mode 0700 and files 0600. Objects publish without replacement
only after a flushed/fsynced temporary file is ready; a matching existing object
is verified rather than overwritten. No plaintext credentials, model keys or
product database access are required. Run artifacts may contain supplied labels
and other private input, so none is automatically exported or included in releases.

The input tree must stay stable during initial freezing. These hashes detect
corruption against recorded identities; they do not authenticate an author or
sandbox a hostile same-user process able to replace directories, edit SQLite,
disable its triggers or reseal all artifacts. Do not open untrusted mutable run
directories in a privileged context. Incomplete initialization may leave a private
directory; it is not a successful run and is never silently overwritten.

## Role and artifact identity

Every case plans one writer, one reviewer depending on the writer, and one
revision depending on that exact writer plus reviewer. Work identity includes
case identity, role and protocol version. Every attempt binds an immutable
request object; results bind request digest and parent artifact digests.

The reviewer consumes the exact saved synthetic writer artifact, never a newly
generated replacement. Revision consumes that same draft and the saved critique.
Ordered units remain ordered structured data; no arbitrary paragraph splitting
is used to fabricate approved publication boundaries. Outputs explicitly carry
synthetic producer semantics and adapter version; there are no scores.

The input projection uses existing allowlisted generator messages and selected
history/evidence references, excluding evaluator labels. Source/evidence objects
are archival references in this adapter, **not materialized into generation text**.
This does not certify that caller-written free-text prompts are free of leakage.
A future real adapter must define source allowlisting, provider settings, egress,
costs and evidence access separately; it cannot simply send every archived object.

## State machine and interruption

Each logical item has at most one attempt in this slice. No automatic retry or
manual resolution command is implemented.

| State | Meaning on resume |
|---|---|
| `planned` | No attempt was reserved; eligible work can start. |
| `reserved` | Request and attempt exist, but dispatch has not started; continue the same attempt. |
| `in_flight` | Dispatch began but no durable result reference exists; recover as `uncertain`. |
| `result_saved` | Result and its ledger reference are durable; verify and complete without invoking again. |
| `completed` | Verify preserved request/result; never invoke again. |
| `failed` | Known synthetic adapter failure; retain in the denominator, no retry. |
| `limited` | Known synthetic adapter limit; retain in the denominator, no retry. |
| `uncertain` | May have executed; preserve unknown completion, no retry. |
| `unavailable` | Role input missing, unknown, invalid or inapplicable; retain the reason, not a pass. |
| `blocked` | A required parent did not complete; no downstream dispatch. |

A crash after object publication but before the ledger records its reference
leaves an orphan object and an uncertain attempt. It is intentionally **not**
inferred complete from that object's existence. A crash after `result_saved`
finishes from the recorded result. Reserved, in-flight, object-saved, result-saved
and completed boundaries are exposed only as library checkpoints for fault tests,
not CLI instructions for executing arbitrary plugins.

This is duplicate-dispatch prevention and explicit uncertainty, not a claim of
universal exactly-once provider execution or zero-cost failure. Live billing,
reservation budgets, retries, scoring and human annotation remain future work.
