"""Build a blinded pairwise panel; the model-to-candidate map stays in custody."""

import hashlib
import json
import os
import re
from itertools import combinations
from pathlib import Path

from ..identity import canonical_bytes, strict_json_loads
from ..workflow import _directory, _fsync
from . import JudgeError
from .criteria import CRITERIA

PANEL_FORMAT = "draftbench-judge-panel-v1"
JUDGE_KINDS = ("typesafe", "openai", "anthropic")
MAX_CANDIDATES = 32
MAX_TEXT_BYTES = 200_000
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def _digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _text(value, limit=MAX_TEXT_BYTES):
    if type(value) is not str or not value.strip():
        raise JudgeError("invalid_text")
    if len(value.encode("utf-8")) > limit:
        raise JudgeError("text_too_large")
    return value


def build_panel(*, brief, candidates, judges):
    """Blind by content digest; candidate order follows the digest, not the model."""
    brief = _text(brief)
    if type(candidates) is not dict or not 2 <= len(candidates) <= MAX_CANDIDATES:
        raise JudgeError("invalid_candidates")
    blinded, custody = {}, {}
    for name, body in candidates.items():
        if type(name) is not str or not _NAME.fullmatch(name):
            raise JudgeError("invalid_candidate_name")
        body = _text(body)
        candidate_id = "c" + _digest({"body": body})[:12]
        if candidate_id in blinded:
            raise JudgeError("duplicate_candidate")
        blinded[candidate_id] = {"body": body}
        custody[candidate_id] = name
    ids = sorted(blinded)
    presentations = []
    for first, second in combinations(ids, 2):
        for a, b in ((first, second), (second, first)):
            presentations.append(
                {"presentation_id": f"{a}-{b}", "a": a, "b": b, "pair": [first, second]}
            )
    panel = {
        "format": PANEL_FORMAT,
        "brief": brief,
        "candidates": {cid: blinded[cid] for cid in ids},
        "criteria": [dict(item) for item in CRITERIA],
        "judges": _judges(judges),
        "presentations": presentations,
    }
    panel["digest"] = _digest(panel)
    return panel, {
        "format": PANEL_FORMAT,
        "panel_digest": panel["digest"],
        "candidates": custody,
    }


def _judges(judges):
    if type(judges) is not list or not 1 <= len(judges) <= 8:
        raise JudgeError("invalid_judges")
    seen = set()
    out = []
    for item in judges:
        if type(item) is not dict or item.get("kind") not in JUDGE_KINDS:
            raise JudgeError("invalid_judges")
        judge_id, model = item.get("judge_id"), item.get("model")
        if (
            type(judge_id) is not str
            or not _NAME.fullmatch(judge_id)
            or judge_id in seen
            or type(model) is not str
            or not _NAME.fullmatch(model)
        ):
            raise JudgeError("invalid_judges")
        seen.add(judge_id)
        entry = {"judge_id": judge_id, "kind": item["kind"], "model": model}
        if item["kind"] == "openai":
            effort = item.get("reasoning_effort", "low")
            if effort not in ("low", "medium", "high"):
                raise JudgeError("invalid_judges")
            entry["reasoning_effort"] = effort
            entry["max_output_tokens"] = _bounded(item.get("max_output_tokens", 4000))
        elif item["kind"] == "anthropic":
            effort = item.get("effort", "low")
            if effort not in ("low", "medium", "high"):
                raise JudgeError("invalid_judges")
            entry["effort"] = effort
            entry["max_tokens"] = _bounded(item.get("max_tokens", 4000))
        if set(item) - set(entry):
            raise JudgeError("invalid_judges")
        out.append(entry)
    return out


def _bounded(value):
    if type(value) is not int or not 256 <= value <= 64_000:
        raise JudgeError("invalid_judges")
    return value


def load_pilot_run(run_dir, *, stage):
    """Read the brief and each model's saved stage output from a pilot run."""
    if stage not in ("writer", "revision"):
        raise JudgeError("invalid_stage")
    root = _directory(run_dir)
    try:
        plan = _read_json(root / "plan.json")
        cases = plan["scope"]["suite_custody"]["cases"]
        if len(cases) != 1:
            raise JudgeError("single_case_required")
        messages = cases[0]["generator"]["writer"]["input"]["messages"]
        brief = "".join(m["content"] for m in messages if m["role"] == "user")
        candidates = {}
        index = "evidence-00000.json" if stage == "writer" else "evidence-00002.json"
        for run in plan["scope"]["runs"]:
            model = run["scope"]["manifest"]["policy"]["model"]
            report = root / "reports" / model
            checksums = _read_json(report / "checksums.json")["files"]
            raw = (report / index).read_bytes()
            if hashlib.sha256(raw).hexdigest() != checksums.get(index):
                raise JudgeError("evidence_checksum_mismatch")
            evidence = strict_json_loads(raw.decode("utf-8"))
            if evidence["role"] != stage or len(evidence["units"]) != 1:
                raise JudgeError("unexpected_evidence")
            content = strict_json_loads(evidence["units"][0]["content"])
            candidates[model] = content["body"]
    except JudgeError:
        raise
    except Exception as exc:
        raise JudgeError("unreadable_run") from exc
    return brief, candidates


def _read_json(path):
    return strict_json_loads(Path(path).read_text(encoding="utf-8"))


def write_panel(output, panel, custody):
    root = _directory(output, create=True)
    for name, value in (("panel.json", panel), ("custody.json", custody)):
        descriptor = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    _fsync(root)
    return root


def read_panel(panel_dir):
    root = _directory(panel_dir)
    try:
        panel = _read_json(root / "panel.json")
    except Exception as exc:
        raise JudgeError("unreadable_panel") from exc
    if (
        type(panel) is not dict
        or panel.get("format") != PANEL_FORMAT
        or panel.get("digest")
        != _digest({k: v for k, v in panel.items() if k != "digest"})
    ):
        raise JudgeError("panel_digest_mismatch")
    return root, panel


def summary(panel):
    return {
        "format": panel["format"],
        "digest": panel["digest"],
        "candidate_count": len(panel["candidates"]),
        "presentation_count": len(panel["presentations"]),
        "criteria": [item["criterion_id"] for item in panel["criteria"]],
        "judges": [
            {"judge_id": j["judge_id"], "kind": j["kind"], "model": j["model"]}
            for j in panel["judges"]
        ],
        "planned_requests": len(panel["presentations"]) * len(panel["judges"]),
    }
