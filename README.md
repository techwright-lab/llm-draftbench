# llm-draftbench

**Benchmark LLM writers, reviewers and revision loops on real content.**

llm-draftbench measures three things separately, because a ranking for one is not a ranking for the others:

1. **Writers** — quality and reliability of a first draft from a frozen brief and evidence packet.
2. **Reviewers** — whether a critic finds real defects, localises them, and leaves acceptable work alone. Issue precision and recall, false passes and false blocks, critical misses, abstention.
3. **Revision** — whether a critique makes the final artifact better under a fixed reviser and budget, judged by an independent evaluator, not by the critic that supplied the notes.

The intended measurement contract carries exact requested and served model, prompt version, settings, evidence hash, judge version, cost and outcome state. The initial offline contract implements a subset of this provenance; see [the contract and its limitations](schemas/CONTRACT.md). Missing measurements are reported as missing, never as a pass or a zero.

## Status

Offline infrastructure, in development. The Python package and CLI are called `draftbench`. They validate local suites, report replayability coverage, exercise synthetic run/resume and compute deterministic checks and reference-conditioned reviewer metrics. The first narrow OpenAI SDK transport is implemented and tested with mocked HTTP only; no real model study or live compatibility test has been run. There is no general semantic quality judge. Private suites, customer material and confirmation data live outside the repository.

## Try the offline example

Install [uv](https://docs.astral.sh/uv/), then from a checkout:

```sh
uv sync --locked
uv run --offline draftbench --help
uv run --offline draftbench validate examples/synthetic/suite.json
uv run --offline draftbench inventory examples/synthetic/suite.json
uv run --offline pytest
```

Dependency installation may need package-index access. Validation, inventory and ordinary tests require no provider account, model calls or product database. The example is explicitly synthetic infrastructure data, not a measured model output.

Use an external suite by passing its manifest path:

```sh
uv run --offline draftbench validate /path/to/private-suite/suite.json
uv run --offline draftbench inventory /path/to/private-suite/suite.json --output /path/to/new-report.json
```

References resolve only within the manifest's directory; remote, traversal and symlink references are rejected. Input trees must remain unchanged during loading. Validation and inventory are read-only by default. Their `--output` exclusively creates a new report and never overwrites an existing file. Validation errors return exit status 2 with safe error codes rather than private input text.

Inventory reports contain aggregate counts and `execution_performed: false`, not quality scores. Historically captured inputs, reconstructions, synthetic inputs and missing data are distinct. A valid manifest is not proof of factual accuracy, redistribution rights or an untouched holdout.

## Exercise synthetic execution and resume

Choose an existing parent and a new run directory outside Git:

```sh
uv run --offline draftbench run examples/smoke/suite.json --adapter fake --output /path/to/new-run
uv run --offline draftbench resume /path/to/new-run
```

The fixed synthetic chain writes a draft once, reviews that exact saved artifact, then revises it using the saved critique. Its private artifact store and SQLite ledger preserve every planned item and attempt. Repeated resume does not repeat completed work. Interrupted in-flight work becomes explicitly uncertain and is not automatically retried.

Add `--max-steps 1` to pause after one synthetic invocation (exit 3); resume completes the rest. Exit 0 means all planned items completed, not that any content passed a quality evaluation. Real evaluation suites are refused by this executor; only explicitly synthetic inputs run. Run/resume currently require POSIX locking. Nothing calls a provider or publishes results.

## Exercise the OpenAI SDK with synthetic HTTP

Install `uv sync --locked --extra openai`, then run:

```sh
uv run --offline --extra openai python examples/openai/smoke.py examples/smoke/suite.json /path/to/new-private-openai-smoke
```

This socket-blocked journey uses the real pinned OpenAI SDK with **in-process
MockTransport**, not a provider account. It verifies pause/resume, exact saved-text
scoring and static report replay. The provider CLI exposes `openai fixture-run`,
`fixture-resume`, `report` and `prepare` as offline operations. Live dispatch is
a separate `pilot run` / `pilot resume` command requiring exact operator approval
and an explicitly selected secure credential file; the smoke never uses it.
See the [OpenAI contract](schemas/OPENAI_CONTRACT.md) for narrow supported snapshots,
conservative reservations, provenance and residual live gates.

## Exercise the optional Inspect SDK offline

`inspect-fixture` runs registered writer/reviewer/revision tasks through the real
pinned Inspect SDK and an in-process fixture transport, never a provider. Install
with `uv sync --locked --extra inspect`. Then run the socket-blocked CLI journey:

```sh
uv run --offline --extra inspect python examples/inspect/smoke.py examples/smoke/suite.json /path/to/new-private-experiment
```

Run/resume, exact-output mechanical rescoring and report replay are exercised.
The optional `run --inspect-policy policy.json` freezes per-call bounds and atomic
request/token-unit/cost admission limits. Concurrency is one, retries and rerolls
are disabled, and uncertain calls keep their reservations. Native usage and cost
stay missing when unavailable; fixtures are never measured models. See the
[Inspect contract and remaining live limitations](schemas/INSPECT_CONTRACT.md).
Inspect has no live-enable flag. Its fixture path never uses the separate
provider-specific pilot launcher or credentials.

## Score explicit references without calling a model

```sh
uv run --offline draftbench score examples/scoring/scoring.json
uv run --offline draftbench score /path/to/private-scoring.json --output /path/to/new-report.json
```

Scoring bundles bind exact artifact units, sources, rubric, reviewer decisions and reference annotations. Mechanical checks enforce explicit lengths/literals and supplied, span-anchored value/unit/citation annotations—not arbitrary prose factuality. Reviewer metrics separate false passes, false blocks, accepted-set risk, coverage, abstention and measurement errors. Finding precision/recall uses explicit attributed alignments, preserving ambiguity for adjudication.

Missing evidence and zero denominators never become perfect scores. Human references, synthetic fixtures, auxiliary model judgments and same-system agreement remain separate cohorts. A successful `score` command means computation completed, not that the content passed or the reference labels are certified gold. Nothing automatically converts product workflow labels into human judgments.

## Collect blinded annotations

```sh
uv run --offline draftbench review export examples/scoring/scoring.json --rubric examples/annotation/rubric.json --raters examples/annotation/raters.json --output /path/to/new-session
uv run --offline draftbench review import /path/to/new-session /path/to/returned-responses.json
uv run --offline draftbench review status /path/to/new-session
```

A separate neutral mandate prevents prior scoring annotations from becoming hints. Consult the private `custody.json` to assign each rater only their randomized packet and editable response template. Do not share the whole session: real identities and mappings stay private. Artifact/source text is preserved, not automatically anonymized.

Imports validate exact artifact, rubric and presentation bindings, retain individual opinions and abstentions, and never overwrite originals. `review records` exports private custodian data; `review adjudicate` appends a separate resolution linked to its original records. Partial inventories remain incomplete, and later opinions make an old resolution non-current. Nothing automatically certifies human gold or applies labels to a newer scoring bundle.

See [the suite contract](schemas/CONTRACT.md), [synthetic run contract](schemas/RUN_CONTRACT.md), [scoring contract](schemas/SCORING_CONTRACT.md), [annotation contract](schemas/ANNOTATION_CONTRACT.md), [benchmark card](BENCHMARK_CARD.md), [contribution guide](CONTRIBUTING.md) and [security policy](SECURITY.md).

## Reproduce a private report and explicitly export locally

After installing the package, this checkout's synthetic builder exercises the real CLI from a paused run through imported fabricated annotations, explicit record selection, report replay and approved local export:

```sh
uv run --offline python examples/reporting/smoke.py /path/to/new-private-experiment
```

The parent directory must already exist and be outside Git. Every generated label and approval is **SYNTHETIC**, not a human judgment. The builder verifies private replay identity and exact public projection parity. Open `report/index.html` locally; `public-local/` is a separate allowlisted export, not a hosted site.

For your saved run, use explicit preparation and binding:

```sh
draftbench report prepare /path/to/run --output /path/to/new-prepared
draftbench review export /path/to/new-prepared/bundle.json --rubric /path/to/new-prepared/rubric.json --raters /path/to/raters.json --output /path/to/new-session
# Import returned responses with review import, then choose record IDs explicitly.
draftbench report render --run /path/to/run --bundle /path/to/new-prepared/bundle.json --bindings /path/to/new-prepared/bindings.json --session /path/to/new-session --selection /path/to/annotation-selection.json --output /path/to/new-private-report
draftbench report replay /path/to/new-private-report --output /path/to/new-replay
draftbench release check /path/to/new-private-report --selection /path/to/release-selection.json
draftbench release export /path/to/new-private-report --approval /path/to/approval.json --output /path/to/new-local-export
```

Preparation scores only completed saved writer/revision artifacts using an explicitly **MECHANICAL ONLY nonempty check**. Plain synthetic critique is not a structured judgment and has no accuracy score. Omit session/selection to retain unknown references; render with only `--run` for operational status, or only `--bundle` for scoring with execution marked `not_run`. Reporting never resumes or dispatches work. Re-preparation is required after run state changes.

Reports contain private JSON, CSV, static script-disabled HTML, local evidence and checksums. Public exports require exact report/projection-bound approval, including approval of aggregate panels; raw evidence additionally requires explicit selection and redistributable component rights. No automatic majority/latest labels, human promotion, recursive copy, network or hosting. Approval is a declared operator attestation, not authenticated identity. See [report and release contracts](schemas/REPORTING_CONTRACT.md) for selection/approval examples and limitations.

## Five-model offline provider support

The exact GPT-6 Astra/Sol/Luna and Claude Opus 5.5/Sonnet 5 IDs have pinned
real-SDK mocked HTTP contracts, shared durable **US$50 total** campaign admission,
and SDK-free saved-output custody/reporting. Both provider families have fixture
CLI routes; there is no ambient credential discovery. The separate three-model
pilot launcher requires explicit scope approval and credentials. All five
pilot policies require explicit enrollment in the same campaign authority.

See [the five-model contract](schemas/FIVE_MODEL_CONTRACT.md) for exact IDs,
versioned pricing provenance, conservative reservation bounds, limitations and
installed-wheel socket-blocked smoke commands. Mocked compatibility is **not**
verified live availability. Archived tariffs do not authorize live dispatch.
Conservative caps can leave runs incomplete rather than overrun the budget.

## What llm-draftbench is not

- Not a universal model leaderboard.
- Not a publication gate. Lab results do not authorize anything in production.
- Not a hosted service. Local-first, file-first, static reports.

## First pilot: prepare offline, run only after human merge

The [operator guide](schemas/PILOT_OPERATOR.md) gives the complete install,
configuration, secure credential-file, prepare/preflight, approval, run/resume and
saved-report commands. No further launcher code is required for the narrow
one-case article pilot; live access, tariff applicability and source-egress
approval remain operator gates, not facts established by offline tests.

```sh
uv sync --locked --extra pilot
uv run --offline draftbench pilot prepare --help
uv run --offline draftbench pilot preflight --plan /path/to/private-run/plan.json
```

Review and **human-merge** the PR first. Then replace the existing dummy `.env`
locally (never overwrite it automatically), finish the independent tariff and
account-route review, prepare the final private plan, and manually approve its
exact digest. Only the separate `pilot run` / `pilot resume` commands dispatch.
`.env.example` is documentation, not a usable credential. Ordinary validation,
fixtures, tests, reports, preparation and preflight never load it.

The pilot fixes Luna, Sol and Sonnet 5, one writer/reviewer/revision chain per
model, low effort, 8,192 output tokens per request, at most nine requests and one
shared US$50 campaign. Exact external output schemas and coarse completeness
checks stop malformed/short results without repair or paid rerolls. This is not
an SEO-quality score or ranking; blind human review and billing reconciliation
come later. Private content-pipeline packets stay outside this repository. The
portable one-user-message envelope is **not exact production-native role replay**.
Nothing automatically publishes content.

## License

Apache-2.0. Datasets, source documents and annotations carry their own terms and are not covered by the code license.
