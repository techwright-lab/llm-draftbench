"""Explicit synthetic scoring inputs; no model or human observations are implied."""

import hashlib
import json

from conftest import seal

from draftbench.scoring.models import ScoreCase


def producer(kind="synthetic", name="fixture-editor"):
    model_state = "unavailable" if kind == "model" else "inapplicable"
    return {
        "kind": kind,
        "name": {"state": "known", "value": name},
        "version": {"state": "known", "value": "1"},
        "requested_model": {"state": model_state, "value": None},
        "served_model": {"state": model_state, "value": None},
    }


def sha(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def score_case_data(text="Synthetic source-grounded draft.", *, case_id="case-1"):
    units = [{"unit_id": "body", "kind": "text", "content": text}]
    binding = {
        "artifact_sha256": sha({"units": units}),
        "rubric_id": "fixture-rubric",
        "rubric_version": "1",
    }
    source = "Synthetic source-grounded draft."
    return {
        "schema_version": "1",
        "case_id": case_id,
        "source_family": f"family-{case_id}",
        "artifact": {"sha256": binding["artifact_sha256"], "units": units},
        "rubric": {
            "rubric_id": "fixture-rubric",
            "version": "1",
            "checks": [
                {
                    "kind": "nonempty",
                    "check_id": "body-present",
                    "applicability": "required",
                }
            ],
        },
        "sources_state": "complete",
        "sources": [
            {
                "source_id": "source-1",
                "text": source,
                "sha256": hashlib.sha256(source.encode()).hexdigest(),
            }
        ],
        "reference": {
            "binding": dict(binding),
            "producer": producer(),
            "basis": "synthetic",
            "independence": "independent",
            "acceptability": "acceptable",
            "defect_inventory": "complete",
            "defects": [],
        },
        "review": {
            "binding": dict(binding),
            "producer": producer(name="fixture-reviewer"),
            "decision": "pass",
            "checks": [{"check_id": "body-present", "state": "pass"}],
            "findings": [],
        },
        "alignment": {
            "binding": dict(binding),
            "producer": producer(),
            "basis": "synthetic",
            "independence": "independent",
            "judgments": [],
        },
    }


def bound_case(data):
    digest = sha({"units": data["artifact"]["units"]})
    data["artifact"]["sha256"] = digest
    for name in ("reference", "review", "alignment"):
        if data[name] is not None:
            data[name]["binding"] = {
                "artifact_sha256": digest,
                "rubric_id": data["rubric"]["rubric_id"],
                "rubric_version": data["rubric"]["version"],
            }
    return ScoreCase.model_validate(seal(data))
