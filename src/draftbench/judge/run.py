"""Dispatch every presentation to every judge with an append-only ledger."""

import json
import os
import sys
import time

from ..credentials import JUDGE_KEYS, load_credentials
from ..workflow import _fsync, _run_lock
from . import JudgeError, generative, jev
from .panel import read_panel, summary
from .report import latest_success, read_ledger

LEDGER_FORMAT = "draftbench-judge-ledger-v1"


def preflight(panel_dir):
    root, panel = read_panel(panel_dir)
    done = latest_success(read_ledger(root))
    out = summary(panel)
    out["completed_requests"] = len(done)
    out["approval_digest"] = panel["digest"]
    return out


def run(
    panel_dir,
    *,
    env_file,
    approve,
    judges=None,
    max_requests=None,
    transports=None,
    progress=None,
    mode="pairwise",
    reps=1,
):
    """Live unless `transports` maps judge ids to fixture transports (tests only).

    Pointwise mode scores each draft alone, so a position cannot bias the answer;
    only typed judges support it. Keys are `pw:<candidate>:<rep>:<judge>`.
    """
    root, panel = read_panel(panel_dir)
    if approve != panel["digest"]:
        raise JudgeError("approval_mismatch")
    if (
        mode not in ("pairwise", "pointwise")
        or type(reps) is not int
        or not 1 <= reps <= 10
    ):
        raise JudgeError("invalid_mode")
    selected = [j for j in panel["judges"] if judges is None or j["judge_id"] in judges]
    if judges is not None and len(selected) != len(set(judges)):
        raise JudgeError("unknown_judge")
    if mode == "pointwise":
        selected = [j for j in selected if j["kind"] == "typesafe"]
        work = [
            {"presentation_id": f"pw:{cid}:{rep}", "candidate": cid, "rep": rep}
            for cid in sorted(panel["candidates"])
            for rep in range(1, reps + 1)
        ]
    else:
        work = panel["presentations"]
    if not selected:
        raise JudgeError("unknown_judge")
    if transports is None:
        from ..adapters.sdk_logging import refuse_sdk_debug_logging

        refuse_sdk_debug_logging()
        credentials = load_credentials(env_file, JUDGE_KEYS)
    else:
        credentials = {}
    if max_requests is not None and (type(max_requests) is not int or max_requests < 1):
        raise JudgeError("invalid_max_requests")
    with _run_lock(root, create=not (root / "run.lock").exists()):
        done = latest_success(read_ledger(root))
        sent = 0
        halted = None
        for judge in selected:
            for presentation in work:
                key = f"{presentation['presentation_id']}:{judge['judge_id']}"
                if key in done:
                    continue
                if max_requests is not None and sent >= max_requests:
                    halted = "max_requests"
                    break
                if progress is not None:
                    progress(
                        {
                            "judge_id": judge["judge_id"],
                            "presentation": presentation["presentation_id"],
                            "state": "sending",
                            "sent": sent,
                        }
                    )
                result = _invoke(
                    judge, panel, presentation, transports, credentials, mode
                )
                sent += 1
                record = {
                    "format": LEDGER_FORMAT,
                    "key": key,
                    "judge_id": judge["judge_id"],
                    "presentation_id": presentation["presentation_id"],
                    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    **result,
                }
                _append(root, record)
                if progress is not None:
                    progress(
                        {
                            "judge_id": judge["judge_id"],
                            "presentation": presentation["presentation_id"],
                            "state": result["status"],
                            "sent": sent,
                            "usage": result.get("usage"),
                            "error_code": result.get("error_code"),
                        }
                    )
                if result["status"] == "success":
                    done[key] = record
                elif result.get("error_code") == "auth_rejected":
                    halted = "auth_rejected"
                    break
            if halted:
                break
    planned = len(work) * len(selected)
    completed = sum(
        1
        for j in selected
        for p in work
        if f"{p['presentation_id']}:{j['judge_id']}" in done
    )
    return {
        "panel_digest": panel["digest"],
        "mode": mode,
        "judges": [j["judge_id"] for j in selected],
        "planned_requests": planned,
        "completed_requests": completed,
        "sent_this_invocation": sent,
        "halted": halted,
        "complete": completed == planned,
        "semantic_quality": "model_judged_not_human",
        "billing_reconciled": False,
    }


def _invoke(judge, panel, presentation, transports, credentials, mode):
    transport = None if transports is None else transports.get(judge["judge_id"])
    if transports is not None and transport is None:
        raise JudgeError("fixture_transport_required")
    if mode == "pointwise":
        return jev.invoke_score(
            judge,
            panel,
            presentation["candidate"],
            transport=transport,
            api_key=credentials.get("DRAFTBENCH_TYPESAFE_API_KEY"),
        )
    if judge["kind"] == "typesafe":
        return jev.invoke(
            judge,
            panel,
            presentation,
            transport=transport,
            api_key=credentials.get("DRAFTBENCH_TYPESAFE_API_KEY"),
        )
    return generative.invoke(
        judge, panel, presentation, transport=transport, credentials=credentials
    )


def _append(root, record):
    line = json.dumps(record, sort_keys=True, allow_nan=False) + "\n"
    descriptor = os.open(
        root / "ledger.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
    )
    with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync(root)


def terminal_progress():
    started = time.monotonic()

    def emit(event):
        usage = event.get("usage") or {}
        line = (
            f"[{time.monotonic() - started:8.1f}s] {event['judge_id']} | {event['presentation']} | "
            f"{event['state']} | sent={event['sent']} | input_tokens={usage.get('input_tokens')} | "
            f"output_tokens={usage.get('output_tokens')}"
            + (f" | error={event['error_code']}" if event.get("error_code") else "")
        )
        try:
            print(line, file=sys.stderr, flush=True)
        except OSError:
            pass

    return emit
