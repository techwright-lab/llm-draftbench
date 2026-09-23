"""Private provider runs, separate from synthetic workflow/report contracts.

Trusted host code supplies an approval callback and an explicit credential for
live dispatch. Files, CLI flags, environment variables and suite data cannot
approve themselves. The separate operator pilot launcher supplies a bound approval
callback only after explicit review; provider CLI routes remain fixture/report-only.
"""

from dataclasses import dataclass
from pathlib import Path

from .adapters.pilot_policy import PilotPolicy
from .adapters.provider_contract import (
    digest,
    native_request,
    parse_policy,
    provider_name,
    run_format,
    transport_contract,
    verify_result,
)
from .identity import canonical_bytes
from .ledger import Ledger
from .loader import _Reader, load_suite
from .models import FileReference
from .provider_reporting import (
    _manifest,
    _prompt,
    _report,
)
from .provider_reporting import (
    prepare_openai as prepare_openai,
)
from .provider_reporting import (
    report_openai as report_openai,
)
from .report import ROLES, generator_payload
from .store import ArtifactStore
from .workflow import _directory, _fsync, _plan, _run_lock

FORMAT = "draftbench-openai-run-v1"
_ISSUER = object()


def invoke(policy, prompt, **kwargs):
    if provider_name(policy) == "anthropic":
        from .adapters.anthropic import invoke as dispatch
    else:
        from .adapters.openai import invoke as dispatch
    return dispatch(policy, prompt, **kwargs)


@dataclass
class DispatchAuthorization:
    """One-use capability issued only after trusted approval AND durable start.

    Python host code is trusted, not sandboxed; never deserialize this capability.
    """

    binding: str
    _used: bool = False
    _issuer: object = None

    def consume(self, policy, prompt):
        expected = digest({"policy": policy.model_dump(mode="json"), "prompt": prompt})
        valid = self._issuer is _ISSUER and not self._used and self.binding == expected
        self._used = True
        return valid


def _input(case, role, reader):
    payload = generator_payload(case, role)
    if payload is None:
        return None
    for item in [payload["source"], *payload["evidence"]]:
        if item.get("file") is not None:
            reference = FileReference.model_validate(item["file"])
            item["content"] = reader.reference(reference, retain=True).decode("utf-8")
    return payload


def _freeze(loaded, policy, fixture, reader):
    return {
        "format": run_format(policy),
        "transport_contract": transport_contract(policy),
        "policy": policy.model_dump(mode="json"),
        "provenance": "fixture" if fixture else "provider",
        "suite_identity": loaded.manifest.identity,
        "case_metadata": {
            case.case_id: {
                "case_identity": case.identity,
                "source_family": case.source_family,
            }
            for case in loaded.cases
        },
        "plan": _plan(loaded.cases),
        # Deliberately exclude evaluator, historical labels and rights text.
        "inputs": {
            case.case_id: {
                role: _input(case, role, reader)
                if getattr(case.generator, role).input is not None
                else None
                for role in ROLES
            }
            for case in loaded.cases
        },
    }


def approval_scope(suite_path, output, policy, *, campaign=None):
    """Private review material only; computes a scope, never grants approval.

    A trusted host must compare its independently approved binding to the callback
    argument. Do not use a callback that blindly returns True or trusts the suite.
    """
    policy = parse_policy(policy)
    loaded = load_suite(suite_path)
    manifest = _freeze(
        loaded, policy, False, _Reader(Path(suite_path).absolute().parent.resolve())
    )
    _bind_campaign(manifest, campaign, policy)
    scope = {"manifest": manifest, "run_directory": str(Path(output).absolute())}
    return {"binding": digest(scope), "scope": scope}


def run_provider(
    suite_path,
    output,
    policy,
    *,
    transport=None,
    approve=None,
    api_key=None,
    max_steps=None,
    checkpoint=None,
    campaign=None,
):
    policy = parse_policy(policy)
    loaded = load_suite(suite_path)
    fixture = transport is not None
    if fixture:
        from .workflow import _validate_synthetic

        _validate_synthetic(loaded)
    manifest = _freeze(
        loaded, policy, fixture, _Reader(Path(suite_path).absolute().parent.resolve())
    )
    _bind_campaign(manifest, campaign, policy)
    if (
        not fixture
        and isinstance(policy, PilotPolicy)
        and policy.verified_tariff_digest is None
    ):
        raise ValueError("verified_tariff_required")
    # Approval is supplied by a trusted embedding host, never from data files.
    # Digest binds exact input corpus, route, prices, currency, caps and run path.
    binding = digest(
        {"manifest": manifest, "run_directory": str(Path(output).absolute())}
    )
    if not fixture and (
        not callable(approve)
        or approve(binding) is not True
        or type(api_key) is not str
        or not api_key
    ):
        raise ValueError("live_authorization_required")
    root = _directory(output, create=True)
    with _run_lock(root, create=True):
        store = ArtifactStore(root / "objects", create=True)
        manifest_digest = store.put_json(manifest)
        with Ledger.create(
            root / "ledger.sqlite3", manifest["plan"], manifest_digest
        ) as ledger:
            _fsync(root)
            return _drive(
                root,
                ledger,
                store,
                manifest,
                transport=transport,
                max_steps=max_steps,
                checkpoint=checkpoint,
                api_key=api_key,
                campaign=campaign,
            )


def resume_provider(
    run_dir,
    *,
    transport=None,
    approve=None,
    api_key=None,
    max_steps=None,
    checkpoint=None,
    campaign=None,
):
    root = _directory(run_dir)
    with _run_lock(root):
        store = ArtifactStore(root / "objects")
        with Ledger.open(root / "ledger.sqlite3") as ledger:
            manifest = _manifest(root, ledger, store)
            if (manifest["provenance"] == "fixture") != (transport is not None):
                raise ValueError("transport_provenance_mismatch")
            binding = digest({"manifest": manifest, "run_directory": str(root)})
            if transport is None and (
                not callable(approve)
                or approve(binding) is not True
                or type(api_key) is not str
                or not api_key
            ):
                raise ValueError("live_authorization_required")
            return _drive(
                root,
                ledger,
                store,
                manifest,
                transport=transport,
                max_steps=max_steps,
                checkpoint=checkpoint,
                api_key=api_key,
                campaign=campaign,
            )


def _bind_campaign(manifest, campaign, policy):
    if isinstance(policy, PilotPolicy) and campaign is None:
        raise ValueError("campaign_required")
    if campaign is not None:
        from .campaign import CampaignBudget

        if type(campaign) is not CampaignBudget:
            raise ValueError("campaign_required")
        if policy.currency != "USD":
            raise ValueError("campaign_currency")
        manifest["campaign"] = campaign.identity


def _drive(
    root,
    ledger,
    store,
    manifest,
    *,
    transport,
    max_steps,
    checkpoint,
    api_key,
    campaign,
):
    expected = manifest.get("campaign")
    if expected is not None or campaign is not None:
        from .campaign import CampaignBudget

        if type(campaign) is not CampaignBudget:
            raise ValueError("campaign_required")
        if campaign.identity != expected:
            raise ValueError("campaign_identity_mismatch")
    policy = parse_policy(manifest["policy"])
    if (
        transport is None
        and isinstance(policy, PilotPolicy)
        and policy.verified_tariff_digest is None
    ):
        raise ValueError("verified_tariff_required")
    if max_steps is not None and (type(max_steps) is not int or max_steps < 0):
        raise ValueError("invalid_max_steps")
    _report(ledger, store, manifest)  # verify saved bindings before any new dispatch
    ledger.recover()
    steps = 0
    for work in ledger.plan():
        state = ledger.state(work["work_id"])
        if state["state"] == "result_saved":
            ledger.complete(state["attempt_id"])
            continue
        if state["state"] not in ("planned", "reserved"):
            continue
        if max_steps is not None and steps >= max_steps:
            break
        if manifest["inputs"][work["case_id"]][work["role"]] is None:
            ledger.skip(work["work_id"], state="unavailable", code="input_unknown")
            continue
        if any(
            ledger.state(dep)["state"] != "completed" for dep in work["dependencies"]
        ):
            ledger.skip(work["work_id"], state="blocked", code="dependency_unresolved")
            continue
        if provider_name(policy) == "anthropic":
            from .adapters.anthropic import preflight
        else:
            from .adapters.openai import preflight

        preflight(transport)
        envelope = _prompt(manifest, work, ledger, store)
        prompt = canonical_bytes(envelope).decode()
        native_request(policy, prompt)  # byte cap before admission or network
        request_digest = store.put_json(envelope)
        if state["state"] == "planned":
            try:
                policy.admit(ledger.summary()["attempt_count"] + 1)
            except ValueError:
                break
            # Whole-run lock serializes admission and reserve; ledger transaction
            # fsyncs reservation before any dispatch. Never refund any attempt.
            attempt = ledger.reserve(work["work_id"], request_digest, policy=policy)
        else:
            attempt = state["attempt_id"]
            if state["request_digest"] != request_digest:
                raise ValueError("request_identity_mismatch")
        if checkpoint:
            checkpoint("reserved", work)
        if campaign is not None:
            # Local reservation supplies a durable UUID, but cannot dispatch.
            # Commit the shared reservation BEFORE local in-flight/start. A
            # crash between databases strands capacity rather than spending it
            # twice; resume reuses the exact attempt/binding without refund.
            try:
                campaign.reserve(
                    attempt_id=attempt,
                    run_digest=digest(
                        {"manifest": manifest, "run_directory": str(root)}
                    ),
                    request_digest=request_digest,
                    config_digest=digest(manifest["policy"]),
                    provider=provider_name(policy),
                    currency=policy.currency,
                    upper_bound=policy.reservation_cost,
                )
            except ValueError as exc:
                if str(exc) != "campaign_exhausted":
                    raise
                break
            if checkpoint:
                checkpoint("campaign_reserved", work)
        ledger.start(attempt)
        if checkpoint:
            checkpoint("in_flight", work)
        authorization = DispatchAuthorization(
            digest({"policy": manifest["policy"], "prompt": prompt}), _issuer=_ISSUER
        )
        result = invoke(
            policy,
            prompt,
            transport=transport,
            authorization=authorization,
            api_key=api_key,
        )
        result.update(
            request_digest=request_digest, parent_digests=envelope["parent_digests"]
        )
        if result["status"] == "success":
            try:
                verify_result(result, policy, prompt, manifest["provenance"])
            except (ValueError, KeyError, TypeError):
                result.update(
                    status="uncertain",
                    charge_status="unknown",
                    cost_upper_estimate=None,
                )
        result_digest = store.put_json(result)
        if checkpoint:
            checkpoint("artifact_saved", work)
        if result["status"] != "success":
            from .workflow import _write_receipt

            _write_receipt(
                root,
                attempt,
                {
                    "attempt_id": attempt,
                    "request_digest": request_digest,
                    "native_digest": result_digest,
                },
            )
            if result["status"] == "uncertain":
                ledger.recover()
            else:
                ledger.fail(
                    attempt,
                    "adapter_limited"
                    if result["status"] == "limited"
                    else "adapter_failed",
                    limited=result["status"] == "limited",
                )
        else:
            ledger.record_result(attempt, result_digest)
            if checkpoint:
                checkpoint("result_saved", work)
            ledger.complete(attempt)
        steps += 1
    return _report(ledger, store, manifest)


# Provider-neutral entry points; legacy OpenAI names remain compatible.
run_openai = run_provider
resume_openai = resume_provider
