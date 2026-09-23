import copy
import json

import pytest
from conftest import seal

from draftbench.cli import main


@pytest.fixture
def all_roles(make_suite, case_data):
    for role in ("reviewer", "revision"):
        case_data["generator"][role] = copy.deepcopy(case_data["generator"]["writer"])
    return make_suite([seal(case_data)])


def test_cli_partial_resume_and_repeat_have_safe_aggregate_reports(
    all_roles, tmp_path, capsys
):
    out = tmp_path / "run"
    assert (
        main(
            [
                "run",
                str(all_roles),
                "--adapter",
                "fake",
                "--output",
                str(out),
                "--max-steps",
                "1",
            ]
        )
        == 3
    )
    partial = json.loads(capsys.readouterr().out)
    assert partial["complete"] is False
    assert partial["states"]["completed"] == 1
    assert main(["resume", str(out)]) == 0
    complete = json.loads(capsys.readouterr().out)
    assert complete["states"]["completed"] == 3
    assert main(["resume", str(out)]) == 0
    assert json.loads(capsys.readouterr().out) == complete
    for marker in (
        str(out),
        "case-1",
        "label-1",
        "First synthetic post",
        "Synthetic source",
    ):
        assert marker not in json.dumps(complete)


@pytest.mark.parametrize("adapter", [None, "openai", "PRIVATE-PROVIDER"])
def test_cli_cannot_dispatch_an_implicit_or_provider_adapter(
    all_roles, tmp_path, adapter, capsys
):
    out = tmp_path / "run"
    args = ["run", str(all_roles), "--output", str(out)]
    if adapter is not None:
        args += ["--adapter", adapter]
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2
    assert json.loads(capsys.readouterr().err) == {
        "valid": False,
        "error": "invalid_arguments",
    }
    assert not out.exists()


def test_cli_masks_invalid_run_state(tmp_path, capsys):
    root = tmp_path / "PRIVATE-RUN"
    root.mkdir(mode=0o700)
    assert main(["resume", str(root)]) == 2
    result = capsys.readouterr()
    assert not result.out
    assert "PRIVATE" not in result.err and "Traceback" not in result.err


def test_cli_refuses_overwrite(all_roles, tmp_path, capsys):
    out = tmp_path / "run"
    command = ["run", str(all_roles), "--adapter", "fake", "--output", str(out)]
    assert main(command) == 0
    before = {
        str(path.relative_to(out)): path.read_bytes()
        for path in out.rglob("*")
        if path.is_file()
    }
    assert main(command) == 2
    assert before == {
        str(path.relative_to(out)): path.read_bytes()
        for path in out.rglob("*")
        if path.is_file()
    }
    assert json.loads(capsys.readouterr().err)["error"] == "run_exists"


def test_invalid_limit_refused_before_output(all_roles, tmp_path, capsys):
    out = tmp_path / "run"
    assert (
        main(
            [
                "run",
                str(all_roles),
                "--adapter",
                "fake",
                "--output",
                str(out),
                "--max-steps",
                "-1",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_step_limit"
    assert not out.exists()
