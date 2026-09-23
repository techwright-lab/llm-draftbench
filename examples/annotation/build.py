"""Build a neutral mandate and fictional roster for the public annotation smoke."""

from pathlib import Path

from draftbench.annotation_models import AnnotationRubric, RaterRoster
from draftbench.identity import canonical_bytes, identity


def producer(name):
    return {
        "kind": "synthetic",
        "name": {"state": "known", "value": name},
        "version": {"state": "known", "value": "1"},
        "requested_model": {"state": "inapplicable", "value": None},
        "served_model": {"state": "inapplicable", "value": None},
    }


def main():
    rubric = {
        "schema_version": "1",
        "format": "draftbench-annotation-rubric-v1",
        "rubric_id": "synthetic-reference-rubric",
        "rubric_version": "1",
        "instructions": "Independently assess the artifact against the supplied source packet. This is a fictional infrastructure fixture. Do not infer facts when evidence is unavailable; use unknown or abstain.",
        "criteria": [
            {
                "criterion_id": "body-present",
                "question": "Does the artifact contain substantive text?",
            },
            {
                "criterion_id": "launch-date",
                "question": "Are date claims supported by the supplied sources?",
            },
        ],
    }
    roster = {
        "schema_version": "1",
        "format": "draftbench-raters-v1",
        "raters": [
            {
                "rater_key": "synthetic-a",
                "producer": producer("Fictional fixture editor A"),
                "independence": "independent",
            },
            {
                "rater_key": "synthetic-b",
                "producer": producer("Fictional fixture editor B"),
                "independence": "independent",
            },
        ],
    }
    directory = Path(__file__).resolve().parent
    for name, data, model in (
        ("rubric", rubric, AnnotationRubric),
        ("raters", roster, RaterRoster),
    ):
        data["identity"] = identity(data)
        model.model_validate(data)
        (directory / f"{name}.json").write_bytes(canonical_bytes(data) + b"\n")
    print("Validated synthetic annotation mandate and fictional rater roster.")


if __name__ == "__main__":
    main()
