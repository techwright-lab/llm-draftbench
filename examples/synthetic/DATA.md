# Synthetic infrastructure fixture

All source text, prompts, draft units and annotation placeholders in this
directory were fabricated for infrastructure validation. They do not describe a
real person, organization, product, model run or publication. The data is dedicated
to the public domain under **CC0-1.0**; the code license does not replace data terms.

This is one development case, not a measured benchmark or hidden confirmation
set. It intentionally has two ordered social-post units, a synthetic writer input,
unknown reviewer availability, an inapplicable revision role, unavailable evidence
and an unknown evaluator answer with synthetic annotation basis. Unknown is not
an answer, a passing score, or gold. Required product-neutral metadata describes
only this fabricated fixture and is not sent to a generator.

The fabricated draft simulates partial **model** history: producer kind remains
model while name/requested-model are unknown and version/served-model are
unavailable. These are null observations, not invented model IDs. No real model
run occurred; this deliberately simulated provenance is not a historical claim.
Original evidence is unavailable, not silently replaced by the supplied source.
The source is available in this example; missing-source states are additionally
exercised by offline regression tests.

From the repository root:

```sh
uv sync --locked
uv run draftbench validate examples/synthetic/suite.json
uv run draftbench inventory examples/synthetic/suite.json
```

The validator must report `valid: true` and `case_count: 1`. Inventory must report
one synthetic writer input, zero historically exact writer inputs and
`execution_performed: false`. Source coverage is one available; evidence coverage
is one unavailable; label counts are one unknown answer and one synthetic basis.
All roles have zero `historical_exact_inputs_ready`. The schema contract is in
`schemas/CONTRACT.md`.

After an intentional edit to this fixture, run:

```sh
uv run --offline python examples/synthetic/reseal.py
uv run --offline python schemas/export.py
```

The resealer recomputes raw artifact hashes, case identities, JSONL hash and suite
identity using the identity utilities, then verifies the suite. It is scoped to
this trusted public fixture, not an importer for arbitrary or private input.

For an external suite, pass its local manifest path instead. No environment file,
credentials, provider setup or repository copy is required. Never commit private
source material or private suite paths in public fixtures.
