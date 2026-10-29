"""Rank candidates from the ledger: Bradley-Terry per judge and criterion.

Ties count half a win each side. Unknown verdicts are counted, never scored.
Position bias is the share of pairs whose two presentations named the same
position instead of the same candidate.
"""

import json
import math
from itertools import combinations

from ..identity import strict_json_loads
from . import JudgeError
from .panel import read_panel

REPORT_FORMAT = "draftbench-judge-report-v1"
SMOOTHING = 0.5
ITERATIONS = 500


def read_ledger(root):
    path = root / "ledger.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(strict_json_loads(line))
    return rows


def latest_success(rows):
    """Last successful record per key wins; an earlier uncertain attempt stays logged."""
    done = {}
    for row in rows:
        if row.get("status") == "success":
            done[row["key"]] = row
    return done


def bradley_terry(ids, comparisons):
    """Minorization-maximization with add-half smoothing; returns log strengths."""
    wins = {i: SMOOTHING * (len(ids) - 1) for i in ids}
    games = {pair: SMOOTHING for pair in combinations(sorted(ids), 2)}
    for winner, loser, weight in comparisons:
        wins[winner] += weight
        games[tuple(sorted((winner, loser)))] += weight
    strength = {i: 1.0 for i in ids}
    for _ in range(ITERATIONS):
        updated = {}
        for i in ids:
            denominator = 0.0
            for j in ids:
                if j == i:
                    continue
                n = games[tuple(sorted((i, j)))]
                denominator += n / (strength[i] + strength[j])
            updated[i] = wins[i] / denominator if denominator else strength[i]
        mean = math.exp(sum(math.log(v) for v in updated.values()) / len(updated))
        strength = {i: v / mean for i, v in updated.items()}
    return {i: round(math.log(v), 4) for i, v in strength.items()}


def _tally(panel, records, judge_id, criterion_id):
    ids = sorted(panel["candidates"])
    comparisons, counts = [], {"a": 0, "b": 0, "tie": 0, "unknown": 0, "missing": 0}
    by_pair = {}
    for presentation in panel["presentations"]:
        key = f"{presentation['presentation_id']}:{judge_id}"
        record = records.get(key)
        verdict = (
            (record or {}).get("verdicts", {}).get(criterion_id) if record else None
        )
        if verdict is None:
            counts["missing"] += 1
            continue
        choice = verdict["choice"]
        counts[choice] += 1
        pair = tuple(presentation["pair"])
        if choice == "a":
            comparisons.append((presentation["a"], presentation["b"], 1.0))
            by_pair.setdefault(pair, []).append(("candidate", presentation["a"], "a"))
        elif choice == "b":
            comparisons.append((presentation["b"], presentation["a"], 1.0))
            by_pair.setdefault(pair, []).append(("candidate", presentation["b"], "b"))
        elif choice == "tie":
            comparisons.append((presentation["a"], presentation["b"], 0.5))
            comparisons.append((presentation["b"], presentation["a"], 0.5))
            by_pair.setdefault(pair, []).append(("tie", None, "tie"))
    consistent = position_biased = 0
    for outcomes in by_pair.values():
        if len(outcomes) != 2:
            continue
        (k1, c1, p1), (k2, c2, p2) = outcomes
        if k1 == "candidate" and k2 == "candidate":
            if c1 == c2:
                consistent += 1
            elif p1 == p2:
                position_biased += 1
        elif k1 == k2 == "tie":
            consistent += 1
    scored = counts["a"] + counts["b"] + counts["tie"]
    return {
        "counts": counts,
        "scored_presentations": scored,
        "pairs_with_both_orders": sum(1 for o in by_pair.values() if len(o) == 2),
        "pairs_consistent": consistent,
        "pairs_position_biased": position_biased,
        "strength": bradley_terry(ids, comparisons) if scored else None,
        "wins": _wins(ids, comparisons),
    }


def _wins(ids, comparisons):
    wins = {i: 0.0 for i in ids}
    for winner, _, weight in comparisons:
        wins[winner] += weight
    return wins


def _agreement(panel, records, judges, criterion_id):
    out = {}
    for first, second in combinations([j["judge_id"] for j in judges], 2):
        same = compared = 0
        for presentation in panel["presentations"]:
            pid = presentation["presentation_id"]
            v1 = (records.get(f"{pid}:{first}") or {}).get("verdicts", {})
            v2 = (records.get(f"{pid}:{second}") or {}).get("verdicts", {})
            c1 = (v1.get(criterion_id) or {}).get("choice")
            c2 = (v2.get(criterion_id) or {}).get("choice")
            if c1 in ("a", "b", "tie") and c2 in ("a", "b", "tie"):
                compared += 1
                same += c1 == c2
        out[f"{first}|{second}"] = {
            "compared": compared,
            "agree": same,
            "rate": round(same / compared, 3) if compared else None,
        }
    return out


def build_report(panel_dir, *, custody=None):
    root, panel = read_panel(panel_dir)
    records = latest_success(read_ledger(root))
    rows = read_ledger(root)
    judges = panel["judges"]
    names = (custody or {}).get("candidates", {})
    results = {}
    for judge in judges:
        per_criterion = {}
        for item in panel["criteria"]:
            per_criterion[item["criterion_id"]] = _tally(
                panel, records, judge["judge_id"], item["criterion_id"]
            )
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "measured_calls": 0,
            "estimated_usd": None,
        }
        total = None
        for record in records.values():
            if record["judge_id"] != judge["judge_id"]:
                continue
            u = record.get("usage")
            if u:
                usage["input_tokens"] += u["input_tokens"]
                usage["output_tokens"] += u["output_tokens"]
                usage["measured_calls"] += 1
            if record.get("estimated_usd") is not None:
                from decimal import Decimal

                total = (total or Decimal(0)) + Decimal(record["estimated_usd"])
        usage["estimated_usd"] = str(total) if total is not None else None
        results[judge["judge_id"]] = {
            "kind": judge["kind"],
            "model": judge["model"],
            "completed_requests": sum(
                1 for r in records.values() if r["judge_id"] == judge["judge_id"]
            ),
            "attempts": sum(1 for r in rows if r["judge_id"] == judge["judge_id"]),
            "usage": usage,
            "criteria": per_criterion,
        }
    agreement = {
        item["criterion_id"]: _agreement(panel, records, judges, item["criterion_id"])
        for item in panel["criteria"]
    }
    return {
        "format": REPORT_FORMAT,
        "panel_digest": panel["digest"],
        "candidates": {cid: names.get(cid) for cid in sorted(panel["candidates"])},
        "planned_requests": len(panel["presentations"]) * len(judges),
        "completed_requests": len(records),
        "judges": results,
        "agreement": agreement,
        "semantic_quality": "model_judged_not_human",
        "billing_reconciled": False,
    }


def render_markdown(report):
    lines = [
        "# Pairwise judge report",
        "",
        f"Panel `{report['panel_digest'][:12]}` · requests {report['completed_requests']}/{report['planned_requests']} · model judgments, not human review.",
        "",
    ]
    names = report["candidates"]
    for judge_id, judge in report["judges"].items():
        lines += [f"## {judge_id} ({judge['kind']} · {judge['model']})", ""]
        usage = judge["usage"]
        lines.append(
            f"Requests {judge['completed_requests']} · input tokens {usage['input_tokens']} · output tokens {usage['output_tokens']} · estimated USD {usage['estimated_usd'] or 'unknown'}"
        )
        lines.append("")
        header = "| Candidate | " + " | ".join(judge["criteria"]) + " |"
        lines += [header, "|" + "---|" * (len(judge["criteria"]) + 1)]
        for cid in sorted(names):
            cells = []
            for tally in judge["criteria"].values():
                strength = tally["strength"]
                cells.append("n/a" if strength is None else f"{strength[cid]:+.2f}")
            lines.append(f"| {names[cid] or cid} | " + " | ".join(cells) + " |")
        lines.append("")
        for criterion_id, tally in judge["criteria"].items():
            c = tally["counts"]
            lines.append(
                f"- {criterion_id}: a={c['a']} b={c['b']} tie={c['tie']} unknown={c['unknown']} missing={c['missing']}; pairs consistent {tally['pairs_consistent']}/{tally['pairs_with_both_orders']}, position-biased {tally['pairs_position_biased']}"
            )
        lines.append("")
    lines += ["## Judge agreement", ""]
    for criterion_id, pairs in report["agreement"].items():
        for key, value in pairs.items():
            lines.append(
                f"- {criterion_id} · {key}: {value['agree']}/{value['compared']} ({value['rate']})"
            )
    lines.append("")
    return "\n".join(lines)


def write_report(panel_dir, output, *, custody=None):
    import os
    from pathlib import Path

    report = build_report(panel_dir, custody=custody)
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
            )
        markdown = Path(output).with_suffix(".md")
        descriptor = os.open(markdown, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(render_markdown(report))
    except FileExistsError as exc:
        raise JudgeError("output_exists") from exc
    return report
