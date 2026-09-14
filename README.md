# llm-draftbench

**Benchmark LLM writers, reviewers and revision loops on real content.**

llm-draftbench measures three things separately, because a ranking for one is not a ranking for the others:

1. **Writers** — quality and reliability of a first draft from a frozen brief and evidence packet.
2. **Reviewers** — whether a critic finds real defects, localises them, and leaves acceptable work alone. Issue precision and recall, false passes and false blocks, critical misses, abstention.
3. **Revision** — whether a critique makes the final artifact better under a fixed reviser and budget, judged by an independent evaluator, not by the critic that supplied the notes.

Every artifact carries its provenance: exact requested and served model, prompt version, settings, evidence hash, judge version, cost and outcome state. Missing measurements are reported as missing, never as a pass or a zero.

## Status

Planning. The package and command-line tool will be called `draftbench`. No code, no measured results, no model has been benchmarked. The repository is public from the start; private suites, customer material and confirmation data live outside it.

## What llm-draftbench is not

- Not a universal model leaderboard.
- Not a publication gate. Lab results do not authorize anything in production.
- Not a hosted service. Local-first, file-first, static reports.

## First pilot

The first suite replays a real content pipeline (TrustGrowth's writer, reviewer and revision workflows) from a read-only export. The toolkit itself is product-agnostic: any system that can produce a draft from a case manifest is a system under test.

## License

Apache-2.0. Datasets, source documents and annotations carry their own terms and are not covered by the code license.
