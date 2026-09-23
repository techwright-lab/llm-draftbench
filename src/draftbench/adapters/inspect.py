"""Offline-only Inspect SDK adapter. No provider factories or credential discovery.

The only transport is an in-process synthetic fixture. Enabling live inference
requires a separately reviewed implementation, not an environment variable.
"""

import asyncio
import hashlib
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..identity import canonical_bytes, strict_json_loads
from ..models import Contract
from .fake import FakeAdapter, FakeResult

SDK_VERSION = "0.3.223"


class InspectPolicy(Contract):
    model: Literal["draftbench-fixture"] = "draftbench-fixture"
    max_requests: Annotated[int, Field(strict=True, ge=1, le=30_000)] = 30_000
    max_input_bytes: Annotated[int, Field(strict=True, ge=1, le=1_000_000)] = 100_000
    max_output_tokens: Annotated[int, Field(strict=True, ge=1, le=100_000)] = 4096
    max_total_tokens: Annotated[int, Field(strict=True, ge=1)] = 4_000_000_000
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=300)] = 30
    max_cost_usd: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = 0
    price_per_million: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = 0

    @model_validator(mode="after")
    def price(self):
        if self.max_cost_usd is not None and self.price_per_million is None:
            raise ValueError("unknown_pricing")
        return self

    @property
    def reserved_tokens(self):
        # Conservative fixture admission units, NOT measured provider tokens.
        return self.max_input_bytes + self.max_output_tokens


def normalize_output(output, *, requested, elapsed):
    reason = output.choices[0].stop_reason if output.choices else None
    status = (
        "error"
        if output.error is not None
        else "limited"
        if reason in ("max_tokens", "model_length")
        else "success"
        if reason == "stop"
        and len(output.choices) == 1
        and not output.choices[0].message.tool_calls
        else "invalid"
    )
    return {
        "sdk_version": SDK_VERSION,
        "requested_model": requested,
        "served_model": output.model,
        "status": status,
        "stop_reason": reason,
        "native_output": output.model_dump(mode="json"),
        "usage": output.usage.model_dump(mode="json") if output.usage else None,
        "latency_seconds": elapsed,
        # Inspect fills output.time with local elapsed time for this transport.
        # Preserve it in native_output, never label fixture time provider latency.
        "provider_latency_seconds": None,
        "cost_usd": None,
        "charge_status": "not_applicable_fixture",
    }


class InspectMetadata(Contract):
    sdk_version: Literal["0.3.223"]
    requested_model: Literal["draftbench-fixture"]
    served_model: Literal["draftbench-fixture"]
    status: Literal["success"]
    stop_reason: Literal["stop"]
    native_output: dict
    usage: dict | None
    latency_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    provider_latency_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    cost_usd: None
    charge_status: Literal["not_applicable_fixture"]
    generation_config: dict
    prompt_sha256: str
    native_request: dict
    dependency_versions: dict[str, str]


class InspectResult(FakeResult):
    adapter: Literal["inspect-fixture"]
    inspect: InspectMetadata

    @model_validator(mode="after")
    def native_binding(self):
        native = self.inspect
        output = native.native_output
        try:
            prompt = native.native_request["input"]
            request = strict_json_loads(prompt)
            policy = InspectPolicy.model_validate(request["inspect_policy"])
            expected = self.model_dump(mode="json", exclude={"inspect"})
            expected["adapter"] = "fake"
            config = native.generation_config
            if (
                strict_json_loads(output["completion"]) != expected
                or len(output["choices"]) != 1
                or output["choices"][0]["message"]["content"] != output["completion"]
                or output["choices"][0]["stop_reason"] != "stop"
                or output.get("error") is not None
                or output["model"] != native.served_model
                or output.get("usage") != native.usage
                or native.provider_latency_seconds is not None
                or hashlib.sha256(prompt.encode()).hexdigest() != self.request_digest
                or native.prompt_sha256 != self.request_digest
                or native.native_request
                != {"input": prompt, "tools": [], "cache": False}
                or any(
                    config.get(key) != value
                    for key, value in safe_config(policy).items()
                )
                or native.dependency_versions.get("inspect-ai") != SDK_VERSION
            ):
                raise ValueError("invalid_native_binding")
        except (KeyError, TypeError, IndexError) as exc:
            raise ValueError("invalid_native_binding") from exc
        return self


class InspectOutcomeError(Exception):
    def __init__(self, native):
        super().__init__("inspect_unsuccessful")
        self.native = native


def _sdk():
    try:
        installed = version("inspect-ai")
    except PackageNotFoundError:
        raise ValueError("inspect_dependency_missing") from None
    if installed != SDK_VERSION:
        raise ValueError("inspect_version_mismatch")
    from inspect_ai.model import GenerateConfig

    return GenerateConfig


def safe_config(policy):
    return dict(
        max_retries=0,
        timeout=policy.timeout_seconds,
        attempt_timeout=policy.timeout_seconds,
        max_connections=1,
        adaptive_connections=False,
        max_tokens=policy.max_output_tokens,
        num_choices=1,
        best_of=1,
        cache=False,
        batch=False,
    )


def generation_config(policy):
    return _sdk()(**safe_config(policy))


def role_task(role, prompt, policy, completion):
    # Lazy import keeps validation/reporting usable without the optional SDK.
    from .inspect_tasks import TASKS

    return TASKS[role](prompt=prompt, policy=policy, completion=completion)


class InspectFixtureAdapter:
    def __init__(self, policy=None):
        self.policy = InspectPolicy.model_validate(policy or {})
        _sdk()

    def invoke(self, request):
        from inspect_ai.model import ChatMessageUser, ModelName
        from inspect_ai.solver import TaskState

        policy = self.policy
        prompt = canonical_bytes(request).decode("utf-8")
        if len(prompt.encode("utf-8")) > policy.max_input_bytes:
            raise InspectOutcomeError(
                {
                    "status": "limited",
                    "error": "input_limit",
                    "charge_status": "not_called",
                }
            )
        fixture = FakeAdapter().invoke(request)
        completion = canonical_bytes(fixture).decode("utf-8")

        task = role_task(request["role"], prompt, policy, completion)
        config = task.config
        model = task.model

        async def call():
            state = TaskState(
                model=ModelName(model),
                sample_id=request["work_id"],
                epoch=1,
                input=prompt,
                messages=[ChatMessageUser(content=prompt)],
            )
            calls = 0

            async def generate_once(state, **kwargs):
                nonlocal calls
                calls += 1
                if calls != 1 or kwargs != {"tool_calls": "none"}:
                    raise ValueError("multiple_generation_forbidden")
                state.output = await model.generate(
                    state.messages, config=config, cache=False
                )
                return state

            state = await asyncio.wait_for(
                task.solver(state, generate_once), timeout=policy.timeout_seconds
            )
            return state.output

        bindings = {
            "generation_config": config.model_dump(mode="json"),
            "dependency_versions": {
                name: version(name)
                for name in ("inspect-ai", "pydantic", "anyio", "tenacity")
            },
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "native_request": {"input": prompt, "tools": [], "cache": False},
        }
        started = time.monotonic()
        try:
            output = asyncio.run(call())
        except Exception as exc:
            raise InspectOutcomeError(
                {
                    **bindings,
                    "sdk_version": SDK_VERSION,
                    "requested_model": policy.model,
                    "served_model": None,
                    "native_output": None,
                    "stop_reason": None,
                    "latency_seconds": time.monotonic() - started,
                    "provider_latency_seconds": None,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "charge_status": "unknown",
                    "usage": None,
                    "cost_usd": None,
                }
            ) from None
        native = normalize_output(
            output, requested=policy.model, elapsed=time.monotonic() - started
        )
        native.update(bindings)
        if output.model != policy.model and native["status"] == "success":
            native["status"] = "invalid"
            native["error"] = "unexpected_served_model"
        if native["status"] != "success":
            raise InspectOutcomeError(native)
        # Parse the EXACT SDK completion, never generate again during normalization.
        from ..identity import strict_json_loads

        try:
            result = FakeResult.model_validate(
                strict_json_loads(output.completion)
            ).model_dump(mode="json")
            result.update(adapter="inspect-fixture", inspect=native)
            return InspectResult.model_validate(result).model_dump(mode="json")
        except (ValueError, TypeError):
            native.update(status="invalid", error="invalid_completion")
            raise InspectOutcomeError(native) from None
