"""Read-only provider custody and preparation; no generation or SDK imports."""

from .adapters.provider_contract import (
    digest,
    native_request,
    parse_policy,
    run_format,
    transport_contract,
    validate_campaign_manifest,
    verify_result,
)
from .identity import canonical_bytes
from .ledger import Ledger
from .store import ArtifactStore
from .workflow import _directory, _run_lock

FORMAT = "draftbench-openai-run-v1"


def _envelope(manifest, work, parents, outputs, external):
    return {
        "input": manifest["inputs"][work["case_id"]][work["role"]],
        "role": work["role"],
        "work_id": work["work_id"],
        "parent_digests": parents,
        "parent_outputs": outputs,
        "external_input": external if work["role"] == "revision" else None,
    }


def _prompt(manifest, work, ledger, store, external=None):
    parents = {dep: ledger.state(dep)["result_digest"] for dep in work["dependencies"]}
    return _envelope(
        manifest,
        work,
        parents,
        {dep: store.get_json(ref)["output"] for dep, ref in parents.items()},
        external,
    )


def work_prompt(work, envelope):
    """Exact native messages sent for one work item, derived from custody only."""
    from .adapters.replay_contract import (
        ROLE_SCHEMAS,
        parse_draft,
        replay_prompt,
        reviewer_messages,
        revision_messages,
    )

    role, packet = work["role"], envelope["input"]
    outputs = envelope["parent_outputs"]
    messages = packet["messages"]
    if role == "reviewer":
        (subject,) = work["dependencies"]
        (exported,) = packet["drafts"]
        messages = reviewer_messages(
            messages, exported["units"][0]["content"], parse_draft(outputs[subject])
        )
    elif role == "revision":
        if envelope["external_input"] is None:
            raise ValueError("review_replay_required")
        draft, review = work["dependencies"]
        messages = revision_messages(
            envelope["external_input"],
            case_id=work["case_id"],
            draft_output=outputs[draft],
            review_output=outputs[review],
            system=messages[0]["content"],
        )
    return replay_prompt(messages, ROLE_SCHEMAS[role])


def _manifest(root, ledger, store):
    manifest = store.get_json(ledger.manifest_digest)
    policy = parse_policy(manifest["policy"])
    validate_campaign_manifest(manifest, policy)
    if (
        manifest.get("format") != run_format(policy)
        or manifest["plan"] != ledger.plan()
    ):
        raise ValueError("invalid_provider_run")
    if manifest["transport_contract"] != transport_contract(policy):
        raise ValueError("provider_contract_mismatch")
    if manifest["provenance"] not in ("fixture", "provider"):
        raise ValueError("invalid_provider_run")
    return manifest


def _report(ledger, store, manifest):
    policy = parse_policy(manifest["policy"])
    summary = ledger.summary()
    for work in ledger.plan():
        state = ledger.state(work["work_id"])
        if state["request_digest"]:
            saved = store.get_json(state["request_digest"])
            envelope = _prompt(
                manifest, work, ledger, store, saved.get("external_input")
            )
            if digest(envelope) != state["request_digest"]:
                raise ValueError("request_identity_mismatch")
            prompt = work_prompt(work, envelope)
            receipt_path = ledger._path.parent / f"attempt-{state['attempt_id']}.json"
            if (
                state["state"] in ("failed", "limited")
                or receipt_path.exists()
                or receipt_path.is_symlink()
            ):
                from .workflow import native_receipt

                receipt = native_receipt(ledger, store, state)
                if (
                    receipt.get("request_digest") != state["request_digest"]
                    or receipt.get("provenance") != manifest["provenance"]
                    or receipt.get("native_request") != native_request(policy, prompt)
                    or receipt.get("parent_digests") != envelope["parent_digests"]
                ):
                    raise ValueError("invalid_provider_receipt")
            if state["result_digest"]:
                result = store.get_json(state["result_digest"])

                verify_result(result, policy, prompt, manifest["provenance"])
                if (
                    result["request_digest"] != state["request_digest"]
                    or result["parent_digests"] != envelope["parent_digests"]
                    or result["provenance"] != manifest["provenance"]
                    or result["status"] != "success"
                    or result["native_request"] != native_request(policy, prompt)
                ):
                    raise ValueError("invalid_provider_result")
    return {
        "format": run_format(policy),
        "report_kind": "provider_execution_summary",
        "provenance": manifest["provenance"],
        "model_execution_performed": False
        if manifest["provenance"] == "fixture"
        else (True if summary["states"]["completed"] else None),
        "complete": summary["states"]["completed"] == summary["work_count"],
        **summary,
        "currency": policy.currency,
        "reserved_cost": str(policy.reservation_total(summary["attempt_count"])),
        "reserved_token_units": policy.reserved_tokens * summary["attempt_count"],
        "billing_reconciled": False,
    }


def prepare_provider(run_dir, output, rights):
    """Bind saved text to mechanical scoring; output rights must be supplied anew.

    This does not infer provider-output rights from source rights. The caller's
    declaration remains private, and grants no publication/egress permission.
    """
    from .annotation import _write_new
    from .models import Rights

    rights = Rights.model_validate(rights).model_dump(mode="json")
    snapshot = snapshot_provider(run_dir)
    bundle, provenance = provider_bundle(snapshot, rights)
    target = _directory(output, create=True)
    _write_new(target / "bundle.json", canonical_bytes(bundle.model_dump(mode="json")))
    _write_new(target / "provider-bindings.json", canonical_bytes(provenance))
    return {
        "prepared_cases": len(bundle.cases),
        "bundle_identity": bundle.identity,
        "provenance": provenance["provenance"],
    }


def snapshot_provider(run_dir):
    """Freeze verified native artifacts under the existing read-only run lock."""
    root = _directory(run_dir)
    with _run_lock(root):
        store = ArtifactStore(root / "objects")
        with Ledger.open(root / "ledger.sqlite3", readonly=True) as ledger:
            manifest = _manifest(root, ledger, store)
            _report(ledger, store, manifest)
            rows = []
            for work in ledger.plan():
                state = ledger.state(work["work_id"])
                rows.append(
                    {
                        **work,
                        **state,
                        "request": store.get_json(state["request_digest"])
                        if state["request_digest"]
                        else None,
                        "result": store.get_json(state["result_digest"])
                        if state["result_digest"]
                        else None,
                    }
                )
            return {
                "manifest": manifest,
                "manifest_digest": ledger.manifest_digest,
                "work": rows,
            }


def provider_bundle(snapshot, rights):
    """Validate frozen custody too, so replay is independent of the source run."""
    from .ledger import STATES
    from .reporting import bundle_from_run

    manifest = snapshot["manifest"]
    policy = parse_policy(manifest["policy"])
    validate_campaign_manifest(manifest, policy)
    if (
        digest(manifest) != snapshot["manifest_digest"]
        or manifest["format"] != run_format(policy)
        or manifest["provenance"] not in ("fixture", "provider")
        or manifest["transport_contract"] != transport_contract(policy)
    ):
        raise ValueError("invalid_provider_snapshot")
    policy = parse_policy(manifest["policy"])
    rows = snapshot["work"]
    by_id = {row["work_id"]: row for row in rows}
    if len(by_id) != len(rows) or len(rows) != len(manifest["plan"]):
        raise ValueError("invalid_provider_snapshot")
    projected = []
    for work, row in zip(manifest["plan"], rows):
        if (
            any(row[key] != value for key, value in work.items())
            or row["state"] not in STATES
        ):
            raise ValueError("invalid_provider_snapshot")
        if row["request_digest"]:
            parents = {dep: by_id[dep]["result_digest"] for dep in work["dependencies"]}
            if type(row["request"]) is not dict:
                raise ValueError("invalid_provider_snapshot")
            envelope = _envelope(
                manifest,
                work,
                parents,
                {dep: by_id[dep]["result"]["output"] for dep in parents},
                row["request"].get("external_input"),
            )
            if row["request"] != envelope or digest(envelope) != row["request_digest"]:
                raise ValueError("request_identity_mismatch")
        elif row["request"] is not None or row["result_digest"]:
            raise ValueError("invalid_provider_snapshot")
        if row["result_digest"]:
            result = row["result"]
            if digest(result) != row["result_digest"]:
                raise ValueError("invalid_provider_snapshot")
            verify_result(
                result, policy, work_prompt(work, envelope), manifest["provenance"]
            )
            if (
                result["request_digest"] != row["request_digest"]
                or result["parent_digests"] != row["request"]["parent_digests"]
            ):
                raise ValueError("invalid_provider_snapshot")
        elif row["result"] is not None or row["state"] == "completed":
            raise ValueError("invalid_provider_snapshot")
        if row["state"] == "completed":
            projected.append(
                {
                    **row,
                    **manifest["case_metadata"][work["case_id"]],
                    "result": {
                        "units": [
                            {
                                "unit_id": row["work_id"],
                                "kind": "text",
                                "content": row["result"]["output"],
                            }
                        ]
                    },
                }
            )
    bundle, bindings = bundle_from_run(
        {
            "work": projected,
            "rights": rights,
            "purpose": "synthetic_infrastructure"
            if manifest["provenance"] == "fixture"
            else "evaluation",
        }
    )
    return bundle, {
        "format": run_format(policy).replace("run-v1", "scoring-bindings-v1"),
        "provenance": manifest["provenance"],
        "manifest_digest": snapshot["manifest_digest"],
        "snapshot_digest": digest(snapshot),
        "bundle_identity": bundle.identity,
        "cases": bindings,
    }


def report_provider(run_dir):
    """Read-only replay; no SDK import, credential access or generation callback."""
    root = _directory(run_dir)
    with _run_lock(root):
        store = ArtifactStore(root / "objects")
        with Ledger.open(root / "ledger.sqlite3", readonly=True) as ledger:
            return _report(ledger, store, _manifest(root, ledger, store))


prepare_openai = prepare_provider
report_openai = report_provider
