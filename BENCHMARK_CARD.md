# Benchmark card

## Purpose and status

llm-draftbench is a product-agnostic toolkit for measuring writers, reviewers and revision workflows separately. This development slice validates suite structure, provenance and replayability coverage offline and exercises a deterministic synthetic writer/reviewer/revision chain with durable run/resume. A separately approved pilot launcher can dispatch a small bounded provider run; no result establishes any model's quality.

TrustGrowth is the first intended private pilot. The public toolkit must not depend on its database, application code, credentials or internal documentation. A system-specific exporter supplies portable manifests from outside this repository.

## Units and evidence

Keep content type, source family, dataset split, exact artifact version and label producer distinct. Related briefs and all their revisions remain in the same source-family split. A family count is not an independently sampled safety denominator unless the sampling design supports it.

Historically captured prompts and reconstructed prompts are different evidence. Missing original evidence, review rounds, served model identity or settings must stay unavailable. Structural validity and matching hashes do not establish factual truth, data rights or exact historical replay.

A model verdict is an observation of reviewer behavior, not independent gold. A published draft is not automatically acceptable; a newer revision is not automatically better. The first pilot is a named-single-editor development study with Ravi until additional independent editorial capacity exists. Reports must state the actual label producers and scope; model judges remain auxiliary.

## Measurement boundaries

- Writer: first-draft quality under frozen inputs and declared resources.
- Reviewer: justified defects and decisions on a fixed artifact, including false blocks, false passes, abstention and failed measurement.
- Revision: independently assessed change from a fixed starting artifact, with original/new defects and resource costs separated.
- Operations: planned, attempted, completed, failed and unassessed work; missing usage is not zero spend.

These are the measurement goals, not claims that every scorer is implemented. Aggregate inventory reports are not model leaderboards. Empty denominators remain undefined. Style cannot compensate for a material factual or policy failure.

## Execution and authorization

Validation, fixtures, scoring and reporting are offline. The only live path is `pilot run` / `pilot resume`, which calls the OpenAI and Anthropic APIs directly through their pinned SDKs on lab-owned accounts; subscription CLIs and intermediary routers are never used. Before a live run, authorize the exact configuration, data egress and budget; configure provider-side caps and local limits. Never use product credentials, default paid fallbacks, contribution-triggered inference or automatic result publication. Local admission controls are not a universal billing guarantee.

The optional Inspect integration is SDK-backed but strictly fixture-only. It
exercises real registered tasks and a local transport under serial, durable
admission limits. Native failures, limits and missing usage remain distinct;
no fixture is a measured model. See [the offline Inspect contract](schemas/INSPECT_CONTRACT.md).
Inspect has no provider transport; live runs use the direct SDK adapters.

## Privacy, rights and release

Private suites, customer material, labels, unredacted traces and identity mappings live outside the repository and public CI. Synthetic public fixtures validate infrastructure only and are not private holdouts or measured model outputs.

The Apache-2.0 code license does not grant rights to datasets, source documents, annotations or embedded third-party material. Retain component-level provenance and permission. Public release is an explicitly reviewed allowlist operation, not a recursive copy of a run directory. Lab results authorize no production action or publication.
