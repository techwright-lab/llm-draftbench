"""Build owned synthetic scorer examples; no observed people/model results."""

import hashlib
from pathlib import Path

from draftbench.identity import canonical_bytes, identity
from draftbench.scoring.models import ScoringBundle, artifact_digest

SOURCE = "The fictional example launch date is 2026-01-01."


def producer(name):
    return {
        "kind": "synthetic",
        "name": {"state": "known", "value": name},
        "version": {"state": "known", "value": "1"},
        "requested_model": {"state": "inapplicable", "value": None},
        "served_model": {"state": "inapplicable", "value": None},
    }


def span(item, text, quote):
    start = text.index(quote)
    return {"item_id": item, "start": start, "end": start + len(quote), "quote": quote}


def make_case(number, date, *, caught=False, unknown=False):
    text = f"An extraordinarily polished fictional example: launch date {date}."
    units = [{"unit_id": "body", "kind": "text", "content": text}]
    artifact_hash = artifact_digest(units)
    target_span = span("body", text, date)
    binding = {
        "artifact_sha256": artifact_hash,
        "rubric_id": "synthetic-reference-rubric",
        "rubric_version": "1",
    }
    incorrect = date != "2026-01-01" and not unknown
    defect = {
        "finding_id": "date-defect",
        "kind": "defect",
        "category": "date",
        "severity": "critical",
        "description": "Synthetic reference-date mismatch.",
        "span": target_span,
    }
    finding = {**defect, "finding_id": "reported-date"}
    reference_producer = None if unknown else producer("synthetic-reference-author")
    data = {
        "schema_version": "1",
        "case_id": f"scoring-fixture-{number}",
        "source_family": "synthetic-unknown-family"
        if unknown
        else "synthetic-launch-family",
        "artifact": {"sha256": artifact_hash, "units": units},
        "rubric": {
            "rubric_id": binding["rubric_id"],
            "version": "1",
            "checks": [
                {
                    "kind": "nonempty",
                    "check_id": "body-present",
                    "applicability": "required",
                },
                {
                    "kind": "fact",
                    "check_id": "launch-date",
                    "applicability": "required",
                    "span": target_span,
                    "observed_value": date,
                    "observed_unit": None,
                    "source_id": None if unknown else "source-1",
                    "reference_span": None
                    if unknown
                    else span("source-1", SOURCE, "2026-01-01"),
                    "expected_value": None if unknown else "2026-01-01",
                    "expected_unit": None,
                    "relevance": "unknown" if unknown else "relevant",
                    "assertion_mode": "asserted",
                    "authorized_nonfactual": False,
                },
            ],
        },
        "sources_state": "unavailable" if unknown else "complete",
        "sources": []
        if unknown
        else [
            {
                "source_id": "source-1",
                "text": SOURCE,
                "sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
            }
        ],
        "reference": {
            "binding": dict(binding),
            "producer": reference_producer,
            "basis": None if unknown else "synthetic",
            "independence": "unknown" if unknown else "independent",
            "acceptability": "unknown"
            if unknown
            else "unacceptable"
            if incorrect
            else "acceptable",
            "defect_inventory": "unavailable" if unknown else "complete",
            "defects": [defect] if incorrect else [],
        },
        "review": {
            "binding": dict(binding),
            "producer": producer("synthetic-reviewer"),
            "decision": "abstain" if unknown else "block" if caught else "pass",
            "checks": [
                {"check_id": "body-present", "state": "pass"},
                {
                    "check_id": "launch-date",
                    "state": "unknown" if unknown else "fail" if caught else "pass",
                },
            ],
            "findings": [finding] if caught else [],
        },
        "alignment": None
        if unknown
        else {
            "binding": dict(binding),
            "producer": producer("synthetic-reference-author"),
            "basis": "synthetic",
            "independence": "independent",
            "judgments": [
                {
                    "finding_id": "reported-date",
                    "state": "matched",
                    "defect_ids": ["date-defect"],
                }
            ]
            if caught
            else [],
        },
    }
    data["identity"] = identity(data)
    return data


def main():
    payload = {
        "schema_version": "1",
        "format": "draftbench-scoring-v1",
        "purpose": "synthetic_infrastructure",
        "rights": {
            "license": "CC0-1.0",
            "usage": "redistributable",
            "authorization": "All source text, draft units and annotations are owned synthetic infrastructure fixtures; no measured model/human results.",
        },
        "cases": [
            make_case(1, "2026-01-01"),
            make_case(2, "2027-01-01"),
            make_case(3, "2028-01-01", caught=True),
            make_case(4, "unknown", unknown=True),
        ],
    }
    payload["identity"] = identity(payload)
    ScoringBundle.model_validate(payload)
    path = Path(__file__).resolve().parent / "scoring.json"
    path.write_bytes(canonical_bytes(payload) + b"\n")
    print(f"Validated {len(payload['cases'])} synthetic scoring cases.")


if __name__ == "__main__":
    main()
