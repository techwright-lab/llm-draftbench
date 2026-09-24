"""Synthetic launcher tests only. conftest denies all sockets, including SDK calls."""

import json

import pytest
from replay_fixtures import (
    ITEMS,
    SIDECAR_CANARY,
    SUITE,
    draft_json,
    review_json,
    review_replay,
)

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
            format="article-pilot-v2",
            account_route="lab",
            organization="org_lab",
            project="proj_lab",
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
    output = tmp_path / "pilot"
    args = dict(
        suite=SUITE,
        config=config,
        tariff=tariff,
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
    assert prepared["max_calls"] == 12
    assert prepared["conservative_reservation_usd"] == "37.01"
    assert prepared["target_words"] == 1200
    assert prepared["account_access_verified"] is False
    scope = json.loads(packet["plan"].read_text())["scope"]
    assert scope["review_item_ids"] == list(ITEMS)
    for run in scope["runs"]:
        policy = run["scope"]["manifest"]["policy"]
        assert policy["max_output_tokens"] == 16000
        assert policy["max_requests"] == 4
        assert SIDECAR_CANARY not in json.dumps(run)


@pytest.mark.parametrize("change", ["config", "tariff", "suite", "code", "plan"])
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


@pytest.mark.parametrize(
    "config",
    [
        {"body_words": [1200, 1600]},
        {"review_item_ids": ["test"]},
        {"format": "article-pilot-v1"},
    ],
)
def test_legacy_config_refused(packet, config):
    value = json.loads(packet["config"].read_text()) | config
    packet["config"].write_text(json.dumps(value))
    with pytest.raises(ValueError, match="invalid_pilot_config"):
        pilot.prepare(**packet)


def test_export_from_other_tg_revision_refused(packet, tmp_path):
    import shutil

    from draftbench.identity import canonical_bytes, identity

    suite = tmp_path / "suite"
    shutil.copytree(SUITE.parent, suite)
    case = json.loads((suite / "cases.jsonl").read_text())
    case["generator"]["reviewer"]["input"]["prompt_version"] = "0badc0de"
    case["identity"] = identity(case)
    body = canonical_bytes(case) + b"\n"
    (suite / "cases.jsonl").write_bytes(body)
    manifest = json.loads((suite / "suite.json").read_text())
    manifest["cases"]["sha256"] = pilot.hashlib.sha256(body).hexdigest()
    manifest["identity"] = identity(manifest)
    (suite / "suite.json").write_bytes(canonical_bytes(manifest))
    packet["suite"] = suite / "suite.json"
    with pytest.raises(ValueError, match="tg_revision_mismatch"):
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
        openai_wire = wire["model"].startswith("gpt")
        if openai_wire:
            reviewer = wire["text"]["format"]["name"] == "ReviewLedgerSchema"
            assert request.url == "https://api.openai.com/v1/responses"
            assert wire["text"]["format"]["strict"] is True
            assert wire["reasoning"] == {"effort": "low"}
            assert wire["max_output_tokens"] == 16000
            user = wire["input"][0]["content"]
        else:
            schema = wire["output_config"]["format"]["schema"]
            reviewer = "results" in schema["properties"]
            assert wire["output_config"]["effort"] == "low"
            assert wire["max_tokens"] == 16000
            assert wire["system"][0]["type"] == "text"
            user = wire["messages"][0]["content"][0]["text"]
        calls.append((wire, user))
        assert len(calls) <= 12
        assert (
            request.headers.get("authorization") == "Bearer " + CANARY
            or request.headers.get("x-api-key") == CANARY
        )
        assert CANARY not in request.content.decode()
        assert SIDECAR_CANARY not in request.content.decode()
        if faults.get("timeout"):
            raise httpx.ReadTimeout("private error " + CANARY, request=request)
        if reviewer:
            content = review_json(faults.get("items", ITEMS))
        else:
            content = draft_json(faults.get("body", "Synthetic lab body."))
        if faults.get("malformed"):
            content = "not json"
        if openai_wire:
            body = {
                "id": "resp_mock",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "model": wire["model"],
                "output": [
                    {
                        "id": "msg_mock",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": content, "annotations": []}
                        ],
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


def answer_replays(output, user="Synthetic TG revision prompt."):
    """Act as the TG no-write review replay for every pending request."""
    for request in sorted((output / "review-replay").glob("*.draft.request.json")):
        value = json.loads(request.read_text())
        response = output / "review-replay" / f"{value['model']}.response.json"
        if not response.exists():
            response.write_text(
                json.dumps(
                    review_replay(
                        json.dumps(value["draft"]),
                        json.dumps(value["ledger"]),
                        user,
                    )
                    | {
                        "draft_sha256": value["draft_sha256"],
                        "review_sha256": value["review_sha256"],
                    }
                )
            )


def pilot_args(packet, tmp_path, approval):
    return [
        "--plan",
        str(packet["plan"]),
        "--env-file",
        str(secret(tmp_path)),
        "--approve",
        approval,
    ]


def test_real_sdk_twelve_calls_one_campaign_resume_and_replay(
    packet, sdk, tmp_path, capsys, monkeypatch
):
    result = pilot.prepare(**packet)
    args = pilot_args(packet, tmp_path, result["approval_digest"])
    assert main(["pilot", "run", *args]) == 3
    waiting = json.loads(capsys.readouterr().out)
    assert waiting["waiting"] == "tg_review_replay_required"
    assert len(waiting["review_replay_requests"]) == 3
    assert len(sdk[0]) == 6
    assert main(["pilot", "resume", *args]) == 3
    assert len(sdk[0]) == 6
    capsys.readouterr()
    answer_replays(packet["output"], user="Synthetic TG revision prompt 7731.")
    assert main(["pilot", "resume", *args]) == 0
    final = json.loads(capsys.readouterr().out)
    assert "waiting" not in final
    assert final["campaign"]["reservation_count"] == 12
    assert len(sdk[0]) == 12
    assert final["length"]["gpt-6-sol"]["revision"] == {
        "body_words": 3,
        "target_words": 1200,
        "ratio_to_target": 0.0025,
    }
    revisions = [user for _, user in sdk[0] if user.endswith("prompt 7731.")]
    assert len(revisions) == 3
    reviews = [user for _, user in sdk[0] if "\n## The draft\n" in user]
    assert len(reviews) == 6
    assert all("Synthetic lab body." in user for user in reviews)
    assert all("Title: Synthetic title\n" in user for user in reviews)
    assert main(["pilot", "resume", *args]) == 0
    assert len(sdk[0]) == 12
    for model in pilot.MODELS:
        snapshot = snapshot_provider(packet["output"] / model)
        assert (
            snapshot["manifest"]["campaign"]["campaign_id"]
            == final["campaign"]["campaign_id"]
        )
        assert [row["state"] for row in snapshot["work"]] == ["completed"] * 4
        assert [row["role"] for row in snapshot["work"]] == [
            "writer",
            "reviewer",
            "revision",
            "reviewer",
        ]
        assert snapshot["work"][1]["request"]["parent_outputs"]
        assert len(snapshot["work"][2]["request"]["parent_outputs"]) == 2
        assert snapshot["work"][2]["request"]["external_input"]["review"]
        assert list(snapshot["work"][3]["request"]["parent_outputs"]) == [
            snapshot["work"][2]["work_id"]
        ]
        assert (
            packet["output"] / "review-replay" / f"{model}.revision.request.json"
        ).exists()
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


@pytest.mark.parametrize("fault", ["timeout", "malformed", "items"])
def test_terminal_outputs_stop_without_retry(packet, sdk, tmp_path, capsys, fault):
    sdk[1][fault] = ITEMS[:1] if fault == "items" else True
    result = pilot.prepare(**packet)
    args = pilot_args(packet, tmp_path, result["approval_digest"])
    expected = 2 if fault == "items" else 1
    assert main(["pilot", "run", *args]) in (2, 3)
    assert len(sdk[0]) == expected
    assert main(["pilot", "resume", *args]) in (2, 3)
    assert len(sdk[0]) == expected
    assert CANARY not in capsys.readouterr().err


@pytest.mark.parametrize("body", ["x", "word " * 5000])
def test_length_is_scored_not_stopped(packet, sdk, tmp_path, capsys, body):
    sdk[1]["body"] = body
    result = pilot.prepare(**packet)
    args = pilot_args(packet, tmp_path, result["approval_digest"])
    assert main(["pilot", "run", *args]) == 3
    assert len(sdk[0]) == 6
    length = json.loads(capsys.readouterr().out)["length"]
    words = len(body.split())
    assert length["claude-sonnet-5"] == {
        "writer": {
            "body_words": words,
            "target_words": 1200,
            "ratio_to_target": round(words / 1200, 4),
        }
    }


@pytest.mark.parametrize("damage", ["binding", "system", "format", "symlink"])
def test_bad_review_replay_refused_before_revision(
    packet, sdk, tmp_path, capsys, damage
):
    result = pilot.prepare(**packet)
    args = pilot_args(packet, tmp_path, result["approval_digest"])
    assert main(["pilot", "run", *args]) == 3
    answer_replays(packet["output"])
    target = packet["output"] / "review-replay" / f"{pilot.MODELS[0]}.response.json"
    value = json.loads(target.read_text())
    if damage == "binding":
        value["draft_sha256"] = "0" * 64
    elif damage == "system":
        value["revision"]["messages"][0]["content"] = "Injected system prompt."
    elif damage == "format":
        value["format"] = "other"
    target.unlink()
    if damage == "symlink":
        real = tmp_path / "elsewhere.json"
        real.write_text(json.dumps(value))
        target.symlink_to(real)
    else:
        target.write_text(json.dumps(value))
    capsys.readouterr()
    assert main(["pilot", "resume", *args]) == 2
    assert len(sdk[0]) == 6
    assert json.loads(capsys.readouterr().err)["error"] == "pilot_refused"


def test_changed_replay_request_refused(packet, sdk, tmp_path):
    result = pilot.prepare(**packet)
    args = pilot_args(packet, tmp_path, result["approval_digest"])
    assert main(["pilot", "run", *args]) == 3
    request = packet["output"] / "review-replay" / "gpt-6-luna.draft.request.json"
    request.write_text("{}")
    assert main(["pilot", "resume", *args]) == 2
    assert len(sdk[0]) == 6


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
        return
    assert result["waiting"] == "tg_review_replay_required"
    assert len(sdk[0]) == 6
    answer_replays(packet["output"])
    result = pilot.run(
        packet["plan"], env_file=env, approve=prepared["approval_digest"], resume=True
    )
    assert result["complete"] is True
    assert len(sdk[0]) == 12
    assert result["campaign"]["reservation_count"] == 12


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
