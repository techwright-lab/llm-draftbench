"""Aggregate coverage only: available inputs are not measured model outcomes."""

from collections import Counter

from .loader import LoadedSuite
from .models import Case, Role

STATES = ("available", "unavailable", "unknown", "inapplicable", "invalid")
BASES = ("historical_exact", "reconstructed", "synthetic")
ROLES = ("writer", "reviewer", "revision")


def inventory(loaded: LoadedSuite) -> dict:
    report = {
        "schema_version": "1",
        "valid": True,
        "report_kind": "offline_replayability_inventory",
        "execution_performed": False,
        "historical_exact_meaning": "Exporter-asserted role input capture only; independent of source/evidence availability, not replay readiness or model reproducibility.",
        "replay_readiness_meaning": "Conservative input completeness only: historical_exact role packet, available source, and available or inapplicable evidence; no execution or full provider-request reproduction.",
        "replay_readiness": {
            role: {"historical_exact_inputs_ready": 0, "not_ready": 0} for role in ROLES
        },
        "source": {state: 0 for state in STATES},
        "case_count": len(loaded.cases),
        "source_family_count": len({case.source_family for case in loaded.cases}),
        "splits": {split: 0 for split in ("development", "confirmation")},
        "roles": {
            role: {state: 0 for state in (*BASES, *STATES[1:])} for role in ROLES
        },
        "evidence": {state: 0 for state in STATES},
        "history": {state: 0 for state in STATES},
        "label_bases": {basis: 0 for basis in BASES},
        "labels": {
            state: 0 for state in ("known", "unknown", "inapplicable", "invalid")
        },
    }
    for case in loaded.cases:
        report["splits"][case.split] += 1
        for role in ROLES:
            role_input = getattr(case.generator, role)
            category = (
                role_input.input.basis
                if role_input.input is not None
                else role_input.state
            )
            report["roles"][role][category] += 1
            ready = (
                category == "historical_exact"
                and case.source.state == "available"
                and case.evidence.state in ("available", "inapplicable")
            )
            report["replay_readiness"][role][
                "historical_exact_inputs_ready" if ready else "not_ready"
            ] += 1
        report["source"][case.source.state] += 1
        report["evidence"][case.evidence.state] += 1
        report["history"][case.history.state] += 1
        for label in case.evaluator.labels:
            report["label_bases"][label.basis] += 1
        counts = Counter(label.state for label in case.evaluator.labels)
        for state, count in counts.items():
            report["labels"][state] += count
    return report


def generator_payload(case: Case, role: Role) -> dict | None:
    """Allowlisted input projection; no labels or producer metadata.

    Does not execute or materialize files. File references retain their hashes;
    a future adapter must explicitly resolve them through the bounded loader.
    Units and messages remain ordered, never flattened into a single string.
    """
    if role not in ROLES:
        raise ValueError("invalid_role")
    role_input = getattr(case.generator, role)
    if role_input.input is None:
        return None
    packet = role_input.input
    evidence = {artifact.artifact_id: artifact for artifact in case.evidence.artifacts}
    drafts = {draft.draft_id: draft for draft in case.history.drafts}
    reviews = {review.review_id: review for review in case.history.reviews}
    return {
        "prompt_version": packet.prompt_version,
        "messages": [message.model_dump() for message in packet.messages],
        "source": {
            "state": case.source.state,
            "file": case.source.artifact.file.model_dump()
            if case.source.artifact is not None
            else None,
        },
        "evidence": [
            {"artifact_id": item, "file": evidence[item].file.model_dump()}
            for item in packet.evidence_ids
        ],
        "drafts": [
            {
                "draft_id": item,
                "version": drafts[item].version,
                "units": [unit.model_dump() for unit in drafts[item].units],
            }
            for item in packet.draft_ids
        ],
        "reviews": [
            {
                "review_id": item,
                "draft_id": reviews[item].draft_id,
                "round": reviews[item].round,
                "feedback": reviews[item].feedback,
            }
            for item in packet.review_ids
        ],
    }
