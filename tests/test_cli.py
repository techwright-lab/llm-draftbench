import json
from pathlib import Path

import pytest
from conftest import seal

from draftbench.cli import main


def test_help(capsys):
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    text = capsys.readouterr().out
    assert "validate" in text and "inventory" in text
    assert "resume" in text and "run" in text


@pytest.mark.parametrize("command", ["validate", "inventory"])
def test_commands_are_offline_safe_and_read_only(command, make_suite, capsys):
    path = make_suite()
    before = {p: p.read_bytes() for p in path.parent.iterdir()}
    assert main([command, str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["case_count"] == 1
    assert report["valid"] is True
    assert {p: p.read_bytes() for p in path.parent.iterdir()} == before
    text = json.dumps(report)
    for secret in [
        str(path),
        "Synthetic source",
        "First synthetic",
        "synthetic-suite",
        "case-1",
    ]:
        assert secret not in text


def test_validation_errors_do_not_echo_inputs(make_suite, case_data, capsys):
    case_data["rights"]["license"] = {"PRIVATE-MARKER": "sensitive input"}
    path = make_suite([seal(case_data)])
    assert main(["validate", str(path)]) == 2
    result = capsys.readouterr()
    assert json.loads(result.err)["error"] == "invalid_case"
    assert "PRIVATE-MARKER" not in result.err
    assert str(path) not in result.err
    assert "Traceback" not in result.err


def test_missing_input_is_safe(tmp_path, capsys):
    path = tmp_path / "PRIVATE-MANIFEST.json"
    assert main(["inventory", str(path)]) == 2
    text = capsys.readouterr().err
    assert "PRIVATE-MANIFEST" not in text
    assert json.loads(text)["error"] == "input_unreadable"


def test_output_explicit_never_clobbers(make_suite, tmp_path, capsys):
    path = make_suite()
    output = tmp_path / "report.json"
    assert main(["inventory", str(path), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["case_count"] == 1
    original = output.read_bytes()
    assert main(["inventory", str(path), "--output", str(output)]) == 2
    assert output.read_bytes() == original
    for target in [path, tmp_path / "cases.jsonl", tmp_path / "source.txt"]:
        before = target.read_bytes()
        assert main(["validate", str(path), "--output", str(target)]) == 2
        assert target.read_bytes() == before
    assert "output_exists" in capsys.readouterr().err


def test_public_sample(capsys):
    example = Path(__file__).parents[1] / "examples" / "synthetic" / "suite.json"
    assert main(["validate", str(example)]) == 0
    assert json.loads(capsys.readouterr().out)["case_count"] == 1
