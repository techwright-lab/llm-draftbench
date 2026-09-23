# Synthetic scoring fixture

The four cases in `scoring.json` are owned **CC0-1.0 synthetic infrastructure
fixtures**. The prose, source, reviewer decisions, defect inventory and alignment
judgments are invented test inputs, not results from a person or model.

They exercise a stipulated correct date, a polished but incorrect date that a
reviewer misses, a different incorrect date that a reviewer catches, and missing
reference evidence with abstention. The date variants share one source family;
they are not independent real-world safety samples. The unknown-reference case
stays separate from the reference-conditioned cohort.

```sh
uv run --offline draftbench score examples/scoring/scoring.json
```

Output is computed from these explicit fixture annotations. A successful command
or a high ratio does not validate any model, human rater or content policy.
`build.py` regenerates and structurally validates only this owned fixture.
See `schemas/SCORING_CONTRACT.md` for exact metric denominators and limitations.
