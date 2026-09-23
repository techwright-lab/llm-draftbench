# Synthetic workflow smoke fixture

This CC0-1.0 development example is derived only from the owned fabricated
`examples/synthetic` fixture. All three role inputs are explicitly synthetic.
No source, draft, critique or label here describes a real model experiment,
person, customer or publication. The fake adapter's outputs are infrastructure
fixtures, not factual/editorial judgments.

Run from the repository root, choosing an existing parent and a **new directory
outside Git**:

```sh
uv sync --locked
uv run --offline draftbench run examples/smoke/suite.json --adapter fake --output /path/to/new-run
uv run --offline draftbench resume /path/to/new-run
```

The fixed chain plans three items: writer, reviewer and revision. A completed
smoke run has three completed items and three attempts. A repeated resume does
not create new attempts. Original source/evidence missingness does not become a
model-quality result just because synthetic execution completes.

To exercise a deliberate pause, add `--max-steps 1` to run: it returns exit 3 with
two pending items. Resume completes the remaining items and returns exit 0. Real
process-death cases are exercised by `tests/test_offline_workflow.py`; uncertain
in-flight attempts are never automatically repeated.

`build.py` reconstructs this owned public smoke data from the adjacent synthetic
fixture and validates it. It never imports private data. See
`schemas/RUN_CONTRACT.md` for persistence, status and safety semantics.
