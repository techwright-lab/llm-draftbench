import copy
import json
from pathlib import Path

import pytest
from conftest import seal
from scoring_cases import bound_case, producer, score_case_data
from test_scoring_contracts import bundle_data

from draftbench.cli import main
from draftbench.scoring.models import ScoringBundle
from draftbench.scoring.reporting import ScoringError, load_scoring_bundle, score_bundle


def write_bundle(tmp_path, cases):
    path = tmp_path / "scoring.json"
    path.write_text(json.dumps(bundle_data(cases)))
    return path


def test_scoring_is_read_only_and_reports_no_private_text(tmp_path, capsys):
    case = bound_case(score_case_data("PRIVATE-MARKER inert content"))
    path = write_bundle(tmp_path, [case])
    before = path.read_bytes()
    assert main(["score", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert path.read_bytes() == before
    assert (
        report["case_count"]
        == report["source_family_count"]
        == report["cohort_count"]
        == 1
    )
    assert report["model_execution_performed"] is False
    assert report["scoring_performed"] is True
    assert report["purpose"] == "synthetic_infrastructure"
    cohort = report["cohorts"][0]
    assert cohort["reference_scope"] == "synthetic_reference"
    assert cohort["deterministic"]["outcomes"]["pass"] == 1
    assert cohort["decisions"]["metrics"]["false_pass_rate"]["value"] is None
    for marker in (
        "PRIVATE-MARKER",
        str(path),
        "case-1",
        "fixture-editor",
        "fixture-rubric",
        "source-1",
    ):
        assert marker not in json.dumps(report)


def test_reference_conditions_are_separate_cohorts_not_one_gold_average(tmp_path):
    synthetic = bound_case(score_case_data())
    other = score_case_data("Different synthetic artifact.", case_id="case-2")
    other["reference"].update(producer=producer("model"), basis="reconstructed")
    other["alignment"].update(producer=producer("model"), basis="reconstructed")
    data = bundle_data([synthetic, bound_case(other)])
    data["purpose"] = "evaluation"
    bundle = ScoringBundle.model_validate(seal(data))
    report = score_bundle(bundle)
    assert report["cohort_count"] == 2
    assert {cohort["reference_scope"] for cohort in report["cohorts"]} == {
        "synthetic_reference",
        "auxiliary_model_agreement",
    }
    assert (
        sum(cohort["decisions"]["counts"]["case_count"] for cohort in report["cohorts"])
        == 2
    )


def test_mutated_nested_bundle_cannot_bypass_identity_validation():
    bundle = ScoringBundle.model_validate(bundle_data([bound_case(score_case_data())]))
    bundle.cases[0].review.checks.clear()
    with pytest.raises((ValueError, ScoringError)):
        score_bundle(bundle)


def test_output_is_exclusive_and_does_not_overwrite_input(tmp_path, capsys):
    path = write_bundle(tmp_path, [bound_case(score_case_data())])
    output = tmp_path / "report.json"
    assert main(["score", str(path), "--output", str(output)]) == 0
    assert output.stat().st_mode & 0o777 == 0o600
    original = output.read_bytes()
    assert main(["score", str(path), "--output", str(output)]) == 2
    assert output.read_bytes() == original
    before = path.read_bytes()
    assert main(["score", str(path), "--output", str(path)]) == 2
    assert path.read_bytes() == before
    assert "output_exists" in capsys.readouterr().err


@pytest.mark.parametrize(
    "payload",
    [b'{"private":"CUSTOMER-MARKER"}', b'{"a":1,"a":2}', b'{"x":1e999}', b"\xff"],
)
def test_cli_errors_do_not_echo_untrusted_content(tmp_path, capsys, payload):
    path = tmp_path / "PRIVATE-FILENAME.json"
    path.write_bytes(payload)
    assert main(["score", str(path)]) == 2
    result = capsys.readouterr()
    assert not result.out
    assert json.loads(result.err)["error"] == "invalid_scoring_bundle"
    assert (
        "PRIVATE" not in result.err
        and "CUSTOMER" not in result.err
        and "Traceback" not in result.err
    )


def test_scoring_loader_rejects_symlink(tmp_path):
    path = write_bundle(tmp_path, [bound_case(score_case_data())])
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(ScoringError):
        load_scoring_bundle(alias)


def test_scoring_schema_matches_executable_models():
    path = Path(__file__).parents[1] / "schemas/scoring.schema.json"
    assert json.loads(path.read_text()) == ScoringBundle.model_json_schema()


def test_conflicting_reference_for_same_observation_cannot_inflate_denominator():
    first = bound_case(score_case_data())
    other = first.model_dump(mode="json")
    other["case_id"] = "conflicting-label"
    other["reference"]["acceptability"] = "unacceptable"
    with pytest.raises(ValueError):
        ScoringBundle.model_validate(
            bundle_data([first, bound_case(copy.deepcopy(other))])
        )
