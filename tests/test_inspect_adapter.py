"""Real SDK, local transport only; never measured model evidence."""

import copy
import socket

import pytest
from conftest import seal

from draftbench.adapters.inspect import InspectFixtureAdapter, InspectPolicy
from draftbench.workflow import resume_synthetic, run_synthetic

pytest.importorskip(
    "inspect_ai", reason="install the inspect extra for SDK fixture tests"
)

_ORIGINAL_SOCKET = socket.socket


@pytest.fixture(autouse=True)
def local_event_loop_sockets(no_network, monkeypatch):
    # asyncio requires an anonymous AF_UNIX socketpair for its wakeup pipe.
    # No creation of network sockets, DNS, connect, or external I/O is allowed.
    def local_only(family=-1, type=-1, proto=-1, fileno=None):
        if family != socket.AF_UNIX or fileno is None:
            raise AssertionError("network forbidden")
        return _ORIGINAL_SOCKET(family, type, proto, fileno=fileno)

    monkeypatch.setattr(socket, "socket", local_only)


@pytest.fixture
def smoke_suite(make_suite, case_data):
    packet = copy.deepcopy(case_data["generator"]["writer"])
    case_data["generator"]["reviewer"] = copy.deepcopy(packet)
    case_data["generator"]["revision"] = copy.deepcopy(packet)
    return make_suite([seal(case_data)])


def test_sdk_chain_and_readonly_rescore(smoke_suite, tmp_path, monkeypatch):
    from draftbench.reporting import build_report, prepare_report, snapshot_run

    out = tmp_path / "run"
    result = run_synthetic(smoke_suite, out, adapter="inspect-fixture")
    assert result["complete"]
    assert result["model_execution_performed"] is False
    snap = snapshot_run(out)
    for row in snap["work"]:
        native = row["result"]["inspect"]
        assert native["sdk_version"] == "0.3.223"
        assert native["status"] == "success"
        assert native["usage"] is None
        # Inspect supplies local SDK timing even with no provider or transport time.
        assert native["native_output"]["time"] is not None
        assert native["native_output"]["time"] >= 0
        assert native["latency_seconds"] >= 0
        assert native["provider_latency_seconds"] is None
        assert (
            native["requested_model"] == native["served_model"] == "draftbench-fixture"
        )
    monkeypatch.setattr(
        InspectFixtureAdapter, "invoke", lambda *a: pytest.fail("regeneration")
    )
    assert resume_synthetic(out)["complete"]
    prepare_report(out, tmp_path / "prepared")
    assert build_report(run_dir=out)


def test_live_route_rejected():
    with pytest.raises(ValueError):
        InspectPolicy(model="openai/anything")


def test_request_budget_is_durable(smoke_suite, tmp_path):
    out = tmp_path / "run"
    policy = InspectPolicy(max_requests=1)
    summary = run_synthetic(
        smoke_suite, out, adapter="inspect-fixture", inspect_policy=policy
    )
    assert summary["attempt_count"] == 1
    assert not summary["complete"]
    assert resume_synthetic(out) == summary


def test_unknown_pricing_rejected():
    with pytest.raises(ValueError):
        InspectPolicy(max_cost_usd=1, price_per_million=None)


@pytest.mark.parametrize("sdk_time", [None, 0.0, 0.125])
@pytest.mark.parametrize("reason", ["stop", "max_tokens", "content_filter", "unknown"])
def test_fixture_timing_preserves_native_without_provider_claim(sdk_time, reason):
    from inspect_ai.model import ChatCompletionChoice, ChatMessageAssistant, ModelOutput

    from draftbench.adapters.inspect import normalize_output

    output = ModelOutput(
        model="draftbench-fixture",
        time=sdk_time,
        choices=[
            ChatCompletionChoice(
                message=ChatMessageAssistant(content="fixture"), stop_reason=reason
            )
        ],
    )
    exact_native = output.model_dump(mode="json")
    result = normalize_output(output, requested="draftbench-fixture", elapsed=0.5)
    assert result["native_output"] == exact_native
    assert output.model_dump(mode="json") == exact_native
    assert result["latency_seconds"] == 0.5
    assert result["provider_latency_seconds"] is None


def test_native_statuses_survive():
    from inspect_ai.model import ChatCompletionChoice, ChatMessageAssistant, ModelOutput

    from draftbench.adapters.inspect import normalize_output

    for reason in ("max_tokens", "model_length", "content_filter", "unknown"):
        output = ModelOutput(
            model="exact-served",
            choices=[
                ChatCompletionChoice(
                    message=ChatMessageAssistant(content="partial"), stop_reason=reason
                )
            ],
        )
        result = normalize_output(output, requested="exact-requested", elapsed=0.1)
        assert result["stop_reason"] == reason
        assert result["status"] != "success"
        assert result["usage"] is None
        assert result["native_output"] == output.model_dump(mode="json")
    output = ModelOutput(model="served", error="native-error")
    assert normalize_output(output, requested="r", elapsed=0)["status"] == "error"


@pytest.mark.parametrize(
    "policy",
    [
        {"max_total_tokens": 1},
        {"max_cost_usd": 0, "price_per_million": 1},
    ],
)
def test_budget_denies_before_dispatch(smoke_suite, tmp_path, monkeypatch, policy):
    monkeypatch.setattr(
        InspectFixtureAdapter, "invoke", lambda *a: pytest.fail("dispatch")
    )
    out = tmp_path / "run"
    result = run_synthetic(
        smoke_suite, out, adapter="inspect-fixture", inspect_policy=policy
    )
    assert result["attempt_count"] == 0
    assert resume_synthetic(out) == result


@pytest.mark.parametrize(
    "kind", ["max_tokens", "model_length", "error", "timeout", "exception"]
)
def test_native_failure_saved_no_retry(smoke_suite, tmp_path, monkeypatch, kind):
    import asyncio

    from inspect_ai.model import ChatCompletionChoice, ChatMessageAssistant, ModelOutput

    from draftbench.adapters.inspect_transport import FixtureAPI
    from draftbench.reporting import snapshot_run

    api = type(FixtureAPI())
    calls = []

    async def fail(self, input, tools, tool_choice, config):
        calls.append(config)
        if kind == "timeout":
            await asyncio.sleep(2)
        if kind == "exception":
            raise RuntimeError("private diagnostic")
        if kind == "error":
            return ModelOutput(model="draftbench-fixture", error="native error")
        return ModelOutput(
            model="draftbench-fixture",
            choices=[
                ChatCompletionChoice(
                    message=ChatMessageAssistant(content="partial"), stop_reason=kind
                )
            ],
        )

    monkeypatch.setattr(api, "generate", fail)
    monkeypatch.setattr(api, "should_retry", lambda *a: True)
    out = tmp_path / "run"
    result = run_synthetic(
        smoke_suite,
        out,
        adapter="inspect-fixture",
        inspect_policy={"timeout_seconds": 1},
    )
    assert len(calls) == 1
    assert calls[0].max_retries == 0
    assert result["attempt_count"] == 1
    assert not result["complete"]
    snap = snapshot_run(out)
    native = snap["work"][0]["native_failure"]
    assert native["provider_latency_seconds"] is None
    assert native["latency_seconds"] >= 0
    if native["native_output"] is not None:
        assert native["native_output"]["time"] is not None
    if kind in ("max_tokens", "model_length"):
        assert native["stop_reason"] == kind
        assert result["states"]["limited"] == 1
    elif kind in ("exception", "timeout"):
        assert native["charge_status"] == "unknown"
        assert native["cost_usd"] is None
    else:
        assert native["native_output"]["error"] == "native error"
    assert resume_synthetic(out) == result
    assert len(calls) == 1


@pytest.mark.parametrize("policy", [{"max_input_bytes": 1}, {"max_output_tokens": 1}])
def test_per_call_limits(smoke_suite, tmp_path, policy):
    out = tmp_path / "run"
    result = run_synthetic(
        smoke_suite, out, adapter="inspect-fixture", inspect_policy=policy
    )
    assert result["states"]["limited"] == 1
    assert result["admission"]["reserved_requests"] == 1
    assert resume_synthetic(out) == result


def test_atomic_reservation_race(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from draftbench.ledger import Ledger, LedgerError

    path = tmp_path / "ledger.sqlite3"
    plan = [
        {"work_id": name, "case_id": "c", "role": "writer", "dependencies": []}
        for name in ("a", "b")
    ]
    with Ledger.create(path, plan, "a" * 64):
        pass

    def reserve(name):
        with Ledger.open(path) as ledger:
            try:
                return ledger.reserve(
                    name, "b" * 64, policy=InspectPolicy(max_requests=1)
                )
            except LedgerError as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["a", "b"]))
    assert results.count("admission_exhausted") == 1
    with Ledger.open(path) as ledger:
        assert ledger.summary()["attempt_count"] == 1


def test_native_binding_tamper_rejected(smoke_suite, tmp_path):
    from draftbench.adapters.inspect import InspectResult
    from draftbench.reporting import snapshot_run

    out = tmp_path / "run"
    run_synthetic(smoke_suite, out, adapter="inspect-fixture")
    original = snapshot_run(out)["work"][0]["result"]
    for key, value in [
        ("served_model", "real-model"),
        ("prompt_sha256", "a" * 64),
        ("status", "limited"),
        ("usage", {"total_tokens": 0}),
        ("provider_latency_seconds", original["inspect"]["native_output"]["time"]),
    ]:
        changed = copy.deepcopy(original)
        changed["inspect"][key] = value
        with pytest.raises(ValueError):
            InspectResult.model_validate(changed)


def test_registered_tasks_have_only_fixture_model():
    from inspect_ai._util.registry import registry_info

    from draftbench.adapters.inspect_tasks import TASKS

    for role, create in TASKS.items():
        task = create()
        assert registry_info(create).type == "task"
        assert task.metadata["role"] == role
        assert task.model.api.model_name == "draftbench-fixture"
        assert task.config.max_connections == 1
        assert task.config.best_of == task.config.num_choices == 1
        assert task.config.max_retries == 0
        assert task.config.cache is task.config.batch is False


@pytest.mark.parametrize(
    "stage,completed,uncertain",
    [
        ("reserved", 3, 0),
        ("in_flight", 0, 1),
        ("artifact_saved", 0, 1),
        ("result_saved", 3, 0),
        ("completed", 3, 0),
    ],
)
def test_inspect_process_crash_never_retries(
    smoke_suite, tmp_path, stage, completed, uncertain
):
    import subprocess
    import sys
    from pathlib import Path

    script = """
import os, sys, runpy
from draftbench.workflow import run_synthetic
scope = runpy.run_path(sys.argv[4])
scope["block_network"]()
def kill(name, context):
    if name == sys.argv[3]:
        os._exit(71)
run_synthetic(sys.argv[1], sys.argv[2], adapter="inspect-fixture", checkpoint=kill)
"""
    out = tmp_path / "run"
    smoke = Path(__file__).resolve().parents[1] / "examples/inspect/smoke.py"
    killed = subprocess.run(
        [sys.executable, "-c", script, str(smoke_suite), str(out), stage, str(smoke)],
        capture_output=True,
        timeout=30,
    )
    assert killed.returncode == 71, killed.stderr.decode()
    summary = resume_synthetic(out)
    assert summary["states"]["completed"] == completed
    assert summary["states"]["uncertain"] == uncertain
    assert summary["admission"]["reservation_released"] is False
    if uncertain:
        assert summary["admission"]["charge_status"] == "unknown"
        assert summary["attempt_count"] == 1
    assert resume_synthetic(out) == summary


def test_missing_sdk_fails_before_creation(smoke_suite, tmp_path, monkeypatch):
    from importlib.metadata import PackageNotFoundError

    from draftbench.adapters import inspect as adapter

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(adapter, "version", missing)
    out = tmp_path / "run"
    with pytest.raises(ValueError, match="inspect_dependency_missing"):
        run_synthetic(smoke_suite, out, adapter="inspect-fixture")
    assert not out.exists()


@pytest.mark.parametrize(
    "served,content,error",
    [
        ("unexpected-model", "fixture", "unexpected_served_model"),
        ("draftbench-fixture", "not-json", "invalid_completion"),
    ],
)
def test_invalid_native_output_is_retained(
    smoke_suite, tmp_path, monkeypatch, served, content, error
):
    from inspect_ai.model import ModelOutput

    from draftbench.adapters.inspect_transport import FixtureAPI
    from draftbench.reporting import snapshot_run

    async def invalid(*args, **kwargs):
        return ModelOutput.from_content(model=served, content=content)

    monkeypatch.setattr(type(FixtureAPI()), "generate", invalid)
    out = tmp_path / "run"
    result = run_synthetic(smoke_suite, out, adapter="inspect-fixture")
    assert result["states"]["failed"] == 1
    native = snapshot_run(out)["work"][0]["native_failure"]
    assert native["served_model"] == served
    assert native["error"] == error
    assert native["native_output"]["completion"] == content
    assert resume_synthetic(out) == result


def test_unbound_result_is_recorded_as_invalid_output(
    smoke_suite, tmp_path, monkeypatch
):
    from draftbench.reporting import snapshot_run

    original = InspectFixtureAdapter.invoke
    calls = []

    def mismatched(self, request):
        calls.append(request["work_id"])
        value = original(self, request)
        value["role"] = "reviewer" if value["role"] == "writer" else "writer"
        return value

    monkeypatch.setattr(InspectFixtureAdapter, "invoke", mismatched)
    out = tmp_path / "run"
    result = run_synthetic(smoke_suite, out, adapter="inspect-fixture")
    assert result["states"]["failed"] == 1
    assert result["states"]["uncertain"] == 0
    assert result["attempt_count"] == 1
    work = snapshot_run(out)["work"][0]
    assert work["error_code"] == "invalid_adapter_output"
    assert work["native_failure"]["status"] == "invalid"
    assert work["native_failure"]["error"] == "invalid_adapter_output"
    assert work["native_failure"]["sdk_version"] == "0.3.223"
    assert resume_synthetic(out) == result
    assert len(calls) == 1


def test_missing_sdk_on_resume_reserves_nothing(smoke_suite, tmp_path, monkeypatch):
    from importlib.metadata import PackageNotFoundError

    from draftbench.adapters import inspect as adapter

    out = tmp_path / "run"
    paused = run_synthetic(smoke_suite, out, adapter="inspect-fixture", max_steps=1)
    real = adapter.version

    def missing(name):
        if name == "inspect-ai":
            raise PackageNotFoundError(name)
        return real(name)

    monkeypatch.setattr(adapter, "version", missing)
    with pytest.raises(ValueError, match="inspect_dependency_missing"):
        resume_synthetic(out)
    monkeypatch.setattr(adapter, "version", real)
    assert resume_synthetic(out, max_steps=0) == paused
