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
  pinned SDKs as the pilot, and `google` (Gemini `generateContent`, v1beta)
  through `google-genai==2.30.1` with `vertexai=False`, one attempt and an
  explicit key, all with a strict `PairwiseVerdictSchema`. Gemini thought
  parts are dropped before parsing; thinking tokens are counted in output. A verdict must
  quote one verbatim sentence from draft A or draft B. A quote found in neither
  draft turns that verdict into `unknown`; it is counted, never scored.

Every judge sees every presentation, including drafts from its own model
family, so self-preference stays measurable. The family-blind views in the
report then drop those verdicts (see Report).

## Run and ledger

`judge run` requires the exact panel digest and an explicit credential file with
all six names (`examples/judge/judge.env.example`). It refuses while
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

Family-blind views need custody. A candidate's family comes from its model
name (`gpt-` OpenAI, `claude-` Anthropic, `gemini-` Google). For each
generative judge, `family_blind` repeats the per-criterion tally without any
presentation that shows a draft from the judge's own family.
`family_blind_consensus` pools those comparisons across the generative judges
into one Bradley-Terry ranking per criterion, so no draft is ranked by a judge
of its own family. Jev has no model family among the candidates and is not
pooled. Cross-family pairs are then judged only by judges outside both
families.

`judge combine --panel A --panel B ... --output FILE` pools the family-blind
comparisons of two or more panels (one per case) by model name into one
ranking per criterion. It also keeps each case's own ranking and the Spearman
rank agreement between every two cases, so a ranking that holds on one brief
only is visible.

## Not established

No human reference, no calibration of any judge, no SEO outcome. A one-case
panel ranks drafts of one brief only. Agreement between judges is not accuracy.
