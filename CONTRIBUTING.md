# Contributing

Use a feature branch and pull request. Do not push directly to the default branch. Include the behavior changed, a focused regression test and the actual verification commands/results.

## Public-only contributions

Use synthetic or expressly redistributable fixtures. Do not submit real customer drafts, private source packets, confirmation cases, credentials, local identity mappings or internal documentation paths. Ignore rules are not data custody. Private suites must remain outside the checkout.

The code license is Apache-2.0. Submit only contributions you have authority to license. Dataset, source and annotation permissions are separate; include their provenance and applicable terms rather than assuming the software license clears them.

## Tests and dependencies

The project uses Python and uv. Use the pinned interpreter and lockfile. Ordinary tests must run without provider credentials, private datasets, product databases or model calls. Add failing tests for changed behavior before implementing it where practical. Dependency installation may require package-index access; runtime tests may not rely on it.

Keep unknown, inapplicable and failed measurements distinct. Preserve source families and exact artifact identity. Never make an aggregate look better by dropping failed attempts or silently changing its denominator.

## Review boundaries

Public CI runs on hosted, unprivileged runners with no provider secrets or private-suite access. Live evaluation and publication are separate maintainer approvals; a passing pull request does not authorize either. Do not add telemetry, default network requests, automatic provider fallback or dynamic untrusted plugins.

Report suspected disclosure or credential-handling vulnerabilities privately as described in SECURITY.md. Ordinary bug reports should contain only minimal synthetic reproductions.
