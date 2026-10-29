"""Explicit offline reconciliation of a saved successful provider response."""

from .adapters.provider_contract import (
    digest,
    native_request,
    parse_policy,
    verify_result,
)
from .ledger import Ledger
from .store import ArtifactStore
from .workflow import _directory, _run_lock, native_receipt


def success_from_receipt(receipt, policy, prompt, provenance):
    if receipt.get("status") != "uncertain" or not isinstance(
        receipt.get("native_output"), dict
    ):
        raise ValueError("successful_native_receipt_required")
    if receipt.get("native_request") != native_request(policy, prompt):
        raise ValueError("receipt_request_mismatch")
    native = receipt["native_output"]
    if receipt.get("adapter") == "openai":
        from .adapters.openai_contract import _usage, visible_output

        if native.get("status") != "completed":
            raise ValueError("successful_native_receipt_required")
        stop = "completed"
    elif receipt.get("adapter") == "anthropic":
        from .adapters.anthropic_contract import _usage, visible_output

        if native.get("stop_reason") != "end_turn":
            raise ValueError("successful_native_receipt_required")
        stop = "end_turn"
    else:
        raise ValueError("unsupported_receipt_adapter")
    usage, estimate = _usage(native, policy)
    result = {k: v for k, v in receipt.items() if k != "error_code"}
    result.update(
        output=visible_output(native, policy),
        usage=usage,
        cost_upper_estimate=estimate,
        status="success",
        stop_reason=stop,
        served_model=native.get("model"),
        completion_id=native.get("id"),
        charge_status="not_applicable_fixture"
        if provenance == "fixture"
        else "unreconciled"
        if usage is not None
        else "unknown",
    )
    verify_result(result, policy, prompt, provenance)
    return result


def authorize_unsent_provider(run_dir, attempt, expected_manifest, *, confirmation):
    from .provider_reporting import _manifest, _report

    if confirmation != "anthropic-empty-bearer-header":
        raise ValueError("explicit_unsent_confirmation_required")
    root = _directory(run_dir)
    with _run_lock(root):
        store = ArtifactStore(root / "objects")
        with Ledger.open(root / "ledger.sqlite3") as ledger:
            manifest = _manifest(root, ledger, store)
            if manifest != expected_manifest:
                raise ValueError("recovery_manifest_mismatch")
            _report(ledger, store, manifest)
            states = [ledger.state(work["work_id"]) for work in ledger.plan()]
            matches = [
                state
                for state in states
                if state["attempt_id"] == attempt and state["state"] == "uncertain"
            ]
            if len(matches) != 1:
                raise ValueError("unsent_attempt_required")
            receipt = native_receipt(ledger, store, matches[0])
            if (
                receipt.get("adapter") != "anthropic"
                or receipt.get("native_output") is not None
                or receipt.get("request_id") is not None
                or receipt.get("error_code") != "provider_connection_error"
            ):
                raise ValueError("unsent_confirmation_incompatible")
            evidence = {
                "format": "draftbench-unsent-retry-v1",
                "attempt_id": attempt,
                "manifest_digest": ledger.manifest_digest,
                "receipt_digest": digest(receipt),
                "operator_confirmation": confirmation,
            }
            ledger.authorize_unsent_retry(
                attempt, digest(receipt), store.put_json(evidence)
            )
            _report(ledger, store, manifest)
            return {
                "retry_authorized": True,
                "attempt_id": attempt,
                "provider_calls": 0,
            }


def reconcile_provider(run_dir, attempt, expected_manifest, *, validate_result):
    from .provider_reporting import _manifest, _prompt, _report, work_prompt

    root = _directory(run_dir)
    with _run_lock(root):
        store = ArtifactStore(root / "objects")
        with Ledger.open(root / "ledger.sqlite3") as ledger:
            manifest = _manifest(root, ledger, store)
            if manifest != expected_manifest:
                raise ValueError("recovery_manifest_mismatch")
            _report(ledger, store, manifest)
            matches = [
                work
                for work in ledger.plan()
                if ledger.state(work["work_id"])["attempt_id"] == attempt
            ]
            if len(matches) != 1:
                raise ValueError("recovery_attempt_missing")
            work = matches[0]
            state = ledger.state(work["work_id"])
            if state["state"] not in ("uncertain", "result_saved", "completed"):
                raise ValueError("recovery_state_refused")
            receipt = native_receipt(ledger, store, state)
            saved = store.get_json(state["request_digest"])
            envelope = _prompt(
                manifest, work, ledger, store, saved.get("external_input")
            )
            if (
                digest(envelope) != state["request_digest"]
                or receipt["parent_digests"] != envelope["parent_digests"]
            ):
                raise ValueError("receipt_request_mismatch")
            result = success_from_receipt(
                receipt,
                parse_policy(manifest["policy"]),
                work_prompt(work, envelope),
                manifest["provenance"],
            )
            if not validate_result(work, result):
                raise ValueError("recovery_output_invalid")
            result_digest = store.put_json(result)
            ledger.reconcile_result(attempt, result_digest, digest(receipt))
            if ledger.state(work["work_id"])["state"] == "result_saved":
                ledger.complete(attempt)
            _report(ledger, store, manifest)
            return {
                "recovered": True,
                "attempt_id": attempt,
                "result_digest": result_digest,
                "provider_calls": 0,
            }
