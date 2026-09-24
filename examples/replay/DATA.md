# Synthetic native-replay fixture

This CC0-1.0 development example has the shape of one TrustGrowth
`draftbench:export` case, with fabricated text only. It is derived from the owned
`examples/synthetic` fixture. No message, draft or label describes a real model
experiment, person, customer or publication.

Each role input is exactly one system and one user message, as TrustGrowth
exports them. The reviewer message holds the exported draft body between
`## The draft` and `## Items` plus the `Title:`/`Meta description:` promise, so
the lab can substitute its own draft. The writer message carries
`- Target Word Count: 1200`. `source.json` stands in for the private source
sidecar; its canary text must never appear in a provider request.

Provider fixture runs plan four calls: writer, reviewer, revision and a reviewer
of the revision. The revision waits for a TrustGrowth review replay response
(`fixture-resume --revision-input FILE`); `examples/providers/smoke.py` builds a
synthetic one. See `schemas/PILOT_OPERATOR.md` for the file formats.

`build.py` reconstructs this data from the adjacent synthetic fixture and
validates it. It never imports private data.
