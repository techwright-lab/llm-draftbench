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
from .ledger import Ledger
from .loader import load_suite
from .provider_reporting import (
    _manifest,
    _prompt,
    _report,
    work_prompt,
)
from .provider_reporting import (
    prepare_openai as prepare_openai,
)
from .provider_reporting import (
    report_openai as report_openai,
)
from .report import ROLES
from .store import ArtifactStore
from .workflow import _directory, _fsync, _run_lock

FORMAT = "draftbench-openai-run-v1"
PROTOCOL = "draftbench-native-replay-v1"
# The second reviewer reviews the revision: 4 calls per case. A reviewer work
# item always reviews its single dependency, which the prompt substitutes in.
STEPS = (
    ("writer", "writer", ()),
    ("reviewer", "reviewer", ("writer",)),
    ("revision", "revision", ("writer", "reviewer")),
    ("revision_review", "reviewer", ("revision",)),
)
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


def _plan(cases):
    work = []
    for case in cases:
        ids = {
            step: digest(
                {"protocol": PROTOCOL, "case_identity": case.identity, "step": step}
            )
            for step, _, _ in STEPS
        }
        for step, role, dependencies in STEPS:
            work.append(
                {
                    "work_id": ids[step],
                    "case_id": case.case_id,
                    "role": role,
                    "dependencies": [ids[dep] for dep in dependencies],
                }
            )
    return work


def _input(case, role):
    """Exported messages only. Source and evidence files never reach a prompt."""
    from .adapters.replay_contract import native_messages

    packet = getattr(case.generator, role).input
    if packet is None:
        return None
    if packet.evidence_ids:
        raise ValueError("evidence_not_supported")
    messages = native_messages([message.model_dump() for message in packet.messages])
    drafts = {draft.draft_id: draft for draft in case.history.drafts}
    exported = [
        {
            "draft_id": item,
            "units": [unit.model_dump() for unit in drafts[item].units],
        }
        for item in packet.draft_ids
    ]
    if role == "reviewer" and (len(exported) != 1 or len(exported[0]["units"]) != 1):
        raise ValueError("reviewer_draft_unavailable")
    return {
        "prompt_version": packet.prompt_version,
        "messages": messages,
        "drafts": exported,
    }


def _freeze(loaded, policy, fixture):
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
        # Deliberately exclude evaluator, historical labels, reviews, rights text
        # and the private source sidecar.
        "inputs": {
            case.case_id: {role: _input(case, role) for role in ROLES}
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
    manifest = _freeze(loaded, policy, False)
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
    revision_input=None,
):
    policy = parse_policy(policy)
    loaded = load_suite(suite_path)
    fixture = transport is not None
    if fixture:
        from .workflow import _validate_synthetic

        _validate_synthetic(loaded)
    manifest = _freeze(loaded, policy, fixture)
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
                revision_input=revision_input,
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
    revision_input=None,
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
                revision_input=revision_input,
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
    revision_input=None,
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
        external = (
            store.get_json(state["request_digest"]).get("external_input")
            if state["request_digest"]
            else revision_input
        )
        if work["role"] == "revision" and external is None:
            break  # Waits for the TG review replay; nothing is reserved.
        envelope = _prompt(manifest, work, ledger, store, external)
        prompt = work_prompt(work, envelope)
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
