# Pairwise judge contract

`draftbench judge` ranks saved drafts of one case against the frozen brief by
blinded pairwise comparison. It is a model judgment, never a human review, and
it never regenerates a draft.

## Panel

`judge prepare` reads either a pilot run (`--run`, the brief from `plan.json`
and each model's saved writer or revision body from `reports/<model>/`,
checksum-verified) or explicit `--brief` and `--candidate NAME=PATH` files.
Each candidate becomes `c` + the first twelve hex digits of the SHA-256 of its
body. `panel.json` holds the brief, the blinded candidates, the five criteria,
the judge list and every presentation: each unordered pair in both orders, so
`n(n-1)` presentations. `custody.json` (0600) is the only map from candidate id
to model name. The panel digest covers everything in `panel.json`; `judge
run --approve` must equal it.

Criteria: `brief_fidelity`, `evidence_discipline`, `clarity`, `insight`,
`publishable`. Every answer is `a`, `b` or `tie`.

## Judges

- `typesafe`: TypeSafe System One `jev-1.14.0` over plain HTTPS
  (`POST /v1/systemone`). One request per presentation carries the brief and
  both drafts as `state` and one `choice` question per criterion. The answer's
  probability distribution and confidence are recorded. Jev cannot quote, so its
  verdicts carry no evidence. Input price is recorded from the public docs as an
  estimate, not an invoice.
- `openai` (Responses API) and `anthropic` (Messages API) through the same
  pinned SDKs as the pilot, with a strict `PairwiseVerdictSchema`. A verdict must
  quote one verbatim sentence from draft A or draft B. A quote found in neither
  draft turns that verdict into `unknown`; it is counted, never scored.

Judge families are not excluded from judging their own family's drafts; the
report shows inter-judge agreement so that bias can be read, not hidden.

## Run and ledger

`judge run` requires the exact panel digest and an explicit credential file with
all five names (`examples/judge/judge.env.example`). It refuses while
`OPENAI_LOG` or `ANTHROPIC_LOG` is set. Every attempt appends one line to
`ledger.jsonl` with the native response; the latest successful record per
`presentation:judge` key wins. Resume skips completed keys. An authentication
rejection halts the run; other failures are recorded and the loop continues.
`--max-requests` bounds one invocation. Nothing prints draft or brief text.

## Report

Per judge and criterion: counts of `a`, `b`, `tie`, `unknown` and missing;
Bradley-Terry log strengths (minorization-maximization, ties as half wins,
add-half smoothing, geometric mean zero); raw weighted wins; and position
consistency: pairs whose two orders named the same candidate versus the same
position. Across judges: pairwise agreement rate per criterion. Usage totals
and the Jev estimate are reported; billing is unreconciled. The report is private
and names models through custody; the panel itself never does.

## Not established

No human reference, no calibration of any judge, no SEO outcome. A one-case
panel ranks drafts of one brief only. Agreement between judges is not accuracy.
