# Annotation infrastructure example

`rubric.json` and `raters.json` are owned **CC0-1.0 synthetic fixtures**. The two
roster entries are fictional, not real human annotators. They use the adjacent
four-case synthetic scoring bundle. No labels or model results are inferred from
these files; exported response templates begin unanswered.

```sh
uv run --offline python examples/annotation/build.py
uv run --offline draftbench review export examples/scoring/scoring.json --rubric examples/annotation/rubric.json --raters examples/annotation/raters.json --output /path/to/new-session
uv run --offline draftbench review status /path/to/new-session
```

Choose a new private directory outside Git. Read only the assignee's `packets/`
JSON and edit their response template; do not share the private session directory.
Import returned responses using `draftbench review import`. Inspect original
records privately with `review records`; use `review adjudicate` for a separately
bound resolution, never a replacement of original opinions.

The integration tests manufacture explicitly synthetic responses to exercise
round-trip identity, spans, timestamps, disagreement and atomicity. Those are
infrastructure tests, not human validation. See `schemas/ANNOTATION_CONTRACT.md`
for data handling and epistemic limits.
