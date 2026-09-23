"""Synthetic launcher tests only. conftest denies all sockets, including SDK calls."""

import json
from pathlib import Path

import pytest

from draftbench import pilot
from draftbench.adapters.pilot_policy import RATES
from draftbench.cli import main
from draftbench.credentials import load_credentials
from draftbench.provider_reporting import snapshot_provider

for _sdk in ("httpx", "openai", "anthropic"):
    pytest.importorskip(_sdk)

CANARY = "sk-canary-never-real-928352"
ENV = (
    f"DRAFTBENCH_OPENAI_API_KEY={CANARY}\n"
    "DRAFTBENCH_OPENAI_ORGANIZATION=org_lab\n"
    "DRAFTBENCH_OPENAI_PROJECT=proj_lab\n"
    f"DRAFTBENCH_ANTHROPIC_API_KEY={CANARY}\n"
)


def secret(tmp_path, text=ENV):
    path = tmp_path / "credentials"
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_explicit_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-must-not-be-read")
    path = secret(tmp_path)
    assert load_credentials(path)["DRAFTBENCH_OPENAI_API_KEY"] == CANARY


@pytest.mark.parametrize(
    "bad",
    [
        ENV.replace(CANARY, "REPLACE_WITH_LAB_KEY"),
        ENV.replace(CANARY, ""),
        ENV + "OPENAI_API_KEY=not-allowed\n",
        ENV + "DRAFTBENCH_OPENAI_API_KEY=duplicate\n",
        ENV.replace(CANARY, "$(touch /should-never-exist)"),
        ENV.replace(CANARY, "${HOME}"),
        ENV.replace(CANARY, "`id`"),
        ENV.replace(CANARY, "'quoted'"),
        ENV.replace(CANARY, '"multiline\nvalue"'),
        ENV.replace(CANARY, "x;id"),
        ENV.replace(CANARY, "x\\\ncontinuation"),
        ENV.replace("DRAFTBENCH_OPENAI_API_KEY", "export DRAFTBENCH_OPENAI_API_KEY"),
        ENV.replace(CANARY, "token # comment"),
        ENV.replace(CANARY, "x\x00y"),
    ],
)
def test_loader_refuses_syntax_without_leak(tmp_path, bad):
    with pytest.raises(ValueError, match="^invalid_explicit_credentials$") as exc:
        load_credentials(secret(tmp_path, bad))
    assert CANARY not in str(exc.value)


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o660, 0o700])
def test_loader_permissions(tmp_path, mode):
    path = secret(tmp_path)
    path.chmod(mode)
    with pytest.raises(ValueError):
        load_credentials(path)


def test_loader_symlink_hardlink_and_owner(tmp_path, monkeypatch):
    import os

    path = secret(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_credentials(link)
    link.unlink()
    os.link(path, link)
    with pytest.raises(ValueError):
        load_credentials(path)
    link.unlink()
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(ValueError):
        load_credentials(path)


@pytest.fixture
def packet(tmp_path):
    def put(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return path

    config = put(
        "config.json",
        dict(
            format="article-pilot-v1",
            account_route="lab",
            organization="org_lab",
            project="proj_lab",
            body_words=[2, 4],
            review_item_ids=["test"],
        ),
    )
    tariff = put(
        "tariff.json",
        dict(
            format="operator-tariff-attestation-v1",
            reviewed=True,
            reviewed_by="SYNTHETIC TEST",
            reviewed_at="SYNTHETIC TEST",
            scope="SYNTHETIC ONLY",
            sources=["https://invalid.test/synthetic"],
            account_route="lab",
            currency="USD",
            rates={m: list(RATES[m]) for m in pilot.MODELS},
        ),
    )
    writer = put(
        "writer.json",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "body", "meta_title", "meta_description", "tags"],
            "properties": {
                **{
                    k: {"type": "string", "minLength": 1}
                    for k in ("title", "body", "meta_title", "meta_description")
                },
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        },
    )
    reviewer = put(
        "reviewer.json",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["results", "factual_issues"],
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["item_id"],
                        "properties": {"item_id": {"type": "string"}},
                    },
                },
                "factual_issues": {"type": "array"},
            },
        },
    )
    output = tmp_path / "pilot"
    args = dict(
        suite=Path(__file__).parent.parent / "examples/smoke/suite.json",
        config=config,
        tariff=tariff,
        writer_schema=writer,
        reviewer_schema=reviewer,
        output=output,
        plan=output / "plan.json",
    )
    return args


def test_prepare_preflight_no_credentials_or_sdk(packet, monkeypatch):
    def denied(*a, **kw):
        raise AssertionError("credentials must not be read")

    monkeypatch.setattr("draftbench.credentials.load_credentials", denied)
    prepared = pilot.prepare(**packet)
    assert pilot.preflight(packet["plan"]) == prepared
    assert prepared["max_calls"] == 9
    assert prepared["account_access_verified"] is False


@pytest.mark.parametrize(
    "change", ["config", "tariff", "writer_schema", "suite", "code", "plan"]
)
def test_changed_plan_refused(packet, change, monkeypatch, tmp_path):
    if change == "suite":
        import shutil

        suite = tmp_path / "suite"
        shutil.copytree(packet["suite"].parent, suite)
        packet["suite"] = suite / "suite.json"
    pilot.prepare(**packet)
    if change == "code":
        monkeypatch.setattr(pilot, "code_identity", lambda: "0" * 64)
    else:
        path = packet[change]
        value = json.loads(path.read_text())
        if change == "plan":
            value["scope"]["max_calls"] = 10
            value["digest"] = pilot.digest(value["scope"])
        else:
            value["extra"] = "changed"
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        pilot.preflight(packet["plan"])


@pytest.mark.parametrize(
    "dependency",
    ["pydantic-core", "jsonschema-specifications", "referencing", "httpcore", "anyio"],
)
@pytest.mark.parametrize("operation", ["preflight", "run", "resume"])
def test_transitive_drift_refused_before_credentials(
    packet, monkeypatch, dependency, operation
):
    from packaging.utils import canonicalize_name

    prepared = pilot.prepare(**packet)
    original = pilot.version

    def drift(name):
        value = original(name)
        return value + "+drift" if canonicalize_name(name) == dependency else value

    def denied(*args, **kwargs):
        raise AssertionError("credentials or dispatch reached")

    monkeypatch.setattr(pilot, "version", drift)
    monkeypatch.setattr("draftbench.credentials.load_credentials", denied)
    for name in ("invoke", "run_provider", "resume_provider"):
        monkeypatch.setattr("draftbench.provider_workflow." + name, denied)
    with pytest.raises(ValueError, match="pilot_plan_changed"):
        if operation == "preflight":
            pilot.preflight(packet["plan"])
        else:
            pilot.run(
                packet["plan"],
                approve=prepared["approval_digest"],
                env_file="must-not-be-opened",
                resume=operation == "resume",
            )


@pytest.mark.parametrize("operation", ["prepare", "preflight", "run", "resume"])
@pytest.mark.parametrize("damage", ["missing", "invalid", "incompatible"])
def test_unresolvable_runtime_refused(packet, monkeypatch, operation, damage):
    prepared = pilot.prepare(**packet) if operation != "prepare" else None
    original = pilot.version

    def broken(name):
        if name == "httpcore":
            if damage == "missing":
                raise pilot.PackageNotFoundError(name)
            return "not-a-version" if damage == "invalid" else "999.0"
        return original(name)

    def denied(*args, **kwargs):
        raise AssertionError("credentials or dispatch reached")

    monkeypatch.setattr(pilot, "version", broken)
    monkeypatch.setattr("draftbench.credentials.load_credentials", denied)
    for name in ("invoke", "run_provider", "resume_provider"):
        monkeypatch.setattr("draftbench.provider_workflow." + name, denied)
    with pytest.raises(ValueError, match="^pilot_dependencies_unresolvable$"):
        if operation == "prepare":
            pilot.prepare(**packet)
        elif operation == "preflight":
            pilot.preflight(packet["plan"])
        else:
            pilot.run(
                packet["plan"],
                approve=prepared["approval_digest"],
                env_file="must-not-be-opened",
                resume=operation == "resume",
            )


def test_remote_schema_refused(packet):
    packet["writer_schema"].write_text(
        json.dumps({"type": "object", "$ref": "https://invalid.test"})
    )
    with pytest.raises(ValueError, match="schema_references_forbidden"):
        pilot.prepare(**packet)


@pytest.mark.parametrize("which", ["dummy", "wrong_digest", "unreviewed", "route"])
def test_run_refuses_before_dispatch(packet, tmp_path, monkeypatch, capsys, which):
    def denied(*args, **kwargs):
        raise AssertionError("dispatch forbidden")

    monkeypatch.setattr("draftbench.provider_workflow.invoke", denied)
    if which == "unreviewed":
        tariff = json.loads(packet["tariff"].read_text())
        tariff["reviewed"] = False
        packet["tariff"].write_text(json.dumps(tariff))
    result = pilot.prepare(**packet)
    env = secret(
        tmp_path,
        ENV.replace(CANARY, "REPLACE_WITH_KEY")
        if which == "dummy"
        else ENV.replace("org_lab", "org_other")
        if which == "route"
        else ENV,
    )
    code = main(
        [
            "pilot",
            "run",
            "--plan",
            str(packet["plan"]),
            "--env-file",
            str(env),
            "--approve",
            "0" * 64 if which == "wrong_digest" else result["approval_digest"],
        ]
    )
    assert code == 2
    assert not (packet["output"] / pilot.MODELS[0]).exists()
    output = capsys.readouterr()
    assert CANARY not in output.out + output.err
    assert json.loads(output.err)["error"] == "pilot_refused"


@pytest.fixture
def sdk(monkeypatch):
    import anthropic  # noqa: F401
    import httpx
    import openai  # noqa: F401 -- initialize real SDK before transport interception

    original = httpx.Client
    calls = []
    faults = {}

    def handler(request):
        wire = json.loads(request.content)
        envelope = json.loads(wire["messages"][0]["content"])
        role = envelope["role"]
        calls.append((wire, envelope))
        assert len(calls) <= 9
        assert (
            request.headers.get("authorization") == "Bearer " + CANARY
            or request.headers.get("x-api-key") == CANARY
        )
        assert CANARY not in request.content.decode()
        assert wire.get("max_completion_tokens", wire.get("max_tokens")) == 8192
        if faults.get("timeout"):
            raise httpx.ReadTimeout("private error " + CANARY, request=request)
        content = json.dumps(
            {"results": [{"item_id": "test"}], "factual_issues": []}
            if role == "reviewer"
            else {
                "title": "Synthetic",
                "body": "synthetic body only",
                "meta_title": "Synthetic",
                "meta_description": "Synthetic",
                "tags": ["test"],
            }
        )
        if faults.get("malformed"):
            content = "not json"
        if faults.get("short"):
            content = content.replace("synthetic body only", "x")
        if wire["model"].startswith("gpt"):
            body = {
                "id": "mock-completion",
                "object": "chat.completion",
                "created": 0,
                "model": wire["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": None,
            }
        else:
            body = {
                "id": "mock-message",
                "type": "message",
                "role": "assistant",
                "model": wire["model"],
                "content": [{"type": "text", "text": content}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": None,
            }
        return httpx.Response(200, json=body)

    class MockClient(original):
        def __init__(self, *args, **kwargs):
            assert kwargs["transport"] is None
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", MockClient)
    return calls, faults


def test_real_sdk_nine_calls_one_campaign_resume_and_replay(
    packet, sdk, tmp_path, capsys, monkeypatch
):
    result = pilot.prepare(**packet)
    env = secret(tmp_path)
    args = [
        "--plan",
        str(packet["plan"]),
        "--env-file",
        str(env),
        "--approve",
        result["approval_digest"],
    ]
    assert main(["pilot", "run", *args]) == 0
    final = json.loads(capsys.readouterr().out)
    assert final["campaign"]["reservation_count"] == 9
    assert len(sdk[0]) == 9
    assert main(["pilot", "resume", *args]) == 0
    assert len(sdk[0]) == 9
    for model in pilot.MODELS:
        snapshot = snapshot_provider(packet["output"] / model)
        assert (
            snapshot["manifest"]["campaign"]["campaign_id"]
            == final["campaign"]["campaign_id"]
        )
        assert [row["state"] for row in snapshot["work"]] == ["completed"] * 3
        assert snapshot["work"][1]["request"]["parent_outputs"]
        assert len(snapshot["work"][2]["request"]["parent_outputs"]) == 2
    assert main(["pilot", "run", *args]) == 2  # never silently restarts
    assert CANARY not in capsys.readouterr().out
    assert all(
        CANARY.encode() not in p.read_bytes()
        for p in packet["output"].rglob("*")
        if p.is_file()
    )
    # Replay/report performs neither SDK imports nor credential loading.
    import builtins

    original = builtins.__import__

    def guard(name, *args, **kwargs):
        if name.split(".")[0] in ("openai", "anthropic", "httpx") or name.endswith(
            "credentials"
        ):
            raise AssertionError("SDK/credential import in replay")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    assert main(["openai", "report", str(packet["output"] / pilot.MODELS[0])]) == 0
    rights = tmp_path / "output-rights.json"
    rights.write_text(
        json.dumps(
            {
                "license": "CC0-1.0",
                "usage": "private",
                "authorization": "Synthetic mocked test output only.",
            }
        )
    )
    prepared_dir, report_dir, replay_dir = [
        tmp_path / name for name in ("prepared", "report", "replay")
    ]
    assert (
        main(
            [
                "openai",
                "prepare",
                str(packet["output"] / pilot.MODELS[0]),
                "--rights",
                str(rights),
                "--output",
                str(prepared_dir),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "report",
                "render",
                "--provider-run",
                str(packet["output"] / pilot.MODELS[0]),
                "--bundle",
                str(prepared_dir / "bundle.json"),
                "--bindings",
                str(prepared_dir / "provider-bindings.json"),
                "--output",
                str(report_dir),
            ]
        )
        == 0
    )
    assert main(["report", "replay", str(report_dir), "--output", str(replay_dir)]) == 0
    assert (report_dir / "report.json").read_bytes() == (
        replay_dir / "report.json"
    ).read_bytes()


@pytest.mark.parametrize("fault", ["timeout", "malformed", "short"])
def test_terminal_outputs_stop_without_retry(packet, sdk, tmp_path, capsys, fault):
    sdk[1][fault] = True
    result = pilot.prepare(**packet)
    env = secret(tmp_path)
    args = [
        "--plan",
        str(packet["plan"]),
        "--env-file",
        str(env),
        "--approve",
        result["approval_digest"],
    ]
    assert main(["pilot", "run", *args]) in (2, 3)
    assert len(sdk[0]) == 1
    assert main(["pilot", "resume", *args]) in (2, 3)
    assert len(sdk[0]) == 1
    assert CANARY not in capsys.readouterr().err


@pytest.mark.parametrize(
    "phase", ["reserved", "campaign_reserved", "in_flight", "result_saved"]
)
def test_resume_crash_boundaries(packet, sdk, tmp_path, monkeypatch, phase):
    import draftbench.provider_workflow as workflow

    prepared = pilot.prepare(**packet)
    env = secret(tmp_path)
    original = workflow.run_provider

    def interrupted(*args, **kwargs):
        def checkpoint(point, work):
            if point == phase:
                raise KeyboardInterrupt("simulated process interruption")

        return original(*args, **kwargs, checkpoint=checkpoint)

    monkeypatch.setattr(workflow, "run_provider", interrupted)
    with pytest.raises(KeyboardInterrupt):
        pilot.run(packet["plan"], env_file=env, approve=prepared["approval_digest"])
    monkeypatch.setattr(workflow, "run_provider", original)
    result = pilot.run(
        packet["plan"], env_file=env, approve=prepared["approval_digest"], resume=True
    )
    if phase == "in_flight":
        assert result["complete"] is False
        assert len(sdk[0]) == 0
    else:
        assert result["complete"] is True
        assert len(sdk[0]) == 9
        assert result["campaign"]["reservation_count"] == 9


def test_no_sdk_imports_during_prepare(packet, monkeypatch):
    import builtins

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in ("openai", "anthropic", "httpx") or name.endswith(
            "credentials"
        ):
            raise AssertionError("forbidden import")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    prepared = pilot.prepare(**packet)
    assert (
        pilot.preflight(packet["plan"])["approval_digest"]
        == prepared["approval_digest"]
    )


def test_low_decimal_context(packet):
    from decimal import Inexact, localcontext

    prepared = pilot.prepare(**packet)
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        assert pilot.preflight(packet["plan"]) == prepared
