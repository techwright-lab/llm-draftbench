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
MODEL_FAMILIES = {
    "openai": ("gpt-",),
    "anthropic": ("claude-",),
    "google": ("gemini-",),
}


def family_of(model):
    for family, prefixes in MODEL_FAMILIES.items():
        if type(model) is str and model.startswith(prefixes):
            return family
    return None


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


def _tally(panel, records, judge_id, criterion_id, *, exclude=frozenset()):
    ids = sorted(set(panel["candidates"]) - exclude)
    comparisons, counts = [], {"a": 0, "b": 0, "tie": 0, "unknown": 0, "missing": 0}
    by_pair = {}
    for presentation in panel["presentations"]:
        if presentation["a"] in exclude or presentation["b"] in exclude:
            continue
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
        "comparisons": comparisons,
    }


def _blind(panel, records, names):
    if not names:
        return {}
    out = {}
    for judge in panel["judges"]:
        if judge["kind"] not in MODEL_FAMILIES:
            continue
        own = frozenset(
            cid
            for cid in panel["candidates"]
            if family_of(names.get(cid)) == judge["kind"]
        )
        out[judge["judge_id"]] = {
            item["criterion_id"]: _tally(
                panel, records, judge["judge_id"], item["criterion_id"], exclude=own
            )
            for item in panel["criteria"]
        }
    return out


def _public(tally):
    return {k: v for k, v in tally.items() if k != "comparisons"}


def _consensus(panel, blind):
    """Pool generative judges; a candidate is ranked only by other-family judges."""
    ids = sorted(panel["candidates"])
    out = {"judges": sorted(blind), "criteria": {}}
    for item in panel["criteria"]:
        criterion_id = item["criterion_id"]
        comparisons = [
            c
            for tallies in blind.values()
            for c in tallies[criterion_id]["comparisons"]
        ]
        out["criteria"][criterion_id] = {
            "scored_presentations": round(sum(w for _, _, w in comparisons)),
            "strength": bradley_terry(ids, comparisons) if comparisons else None,
            "wins": _wins(ids, comparisons),
        }
    return out


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


def spearman(ranking_a, ranking_b):
    """Rank correlation over the candidates present in both maps (higher is better)."""
    ids = sorted(set(ranking_a) & set(ranking_b))
    if len(ids) < 3:
        return None

    def ranks(values):
        order = sorted(ids, key=lambda i: -values[i])
        return {i: order.index(i) + 1 for i in ids}

    ra, rb = ranks(ranking_a), ranks(ranking_b)
    n = len(ids)
    d2 = sum((ra[i] - rb[i]) ** 2 for i in ids)
    return round(1 - 6 * d2 / (n * (n * n - 1)), 3)


def _pointwise(panel, records, judge_id):
    out = {}
    for item in panel["criteria"]:
        cid_scores = {}
        for cid in sorted(panel["candidates"]):
            answers = [
                r["verdicts"][item["criterion_id"]]["score"]
                for key, r in records.items()
                if key.startswith(f"pw:{cid}:") and key.endswith(f":{judge_id}")
            ]
            scores = [score for score in answers if score is not None]
            if scores:
                cid_scores[cid] = {
                    "mean": round(sum(scores) / len(scores), 3),
                    "min": min(scores),
                    "max": max(scores),
                    "reps": len(scores),
                }
                if len(scores) < len(answers):
                    cid_scores[cid]["unknown"] = len(answers) - len(scores)
            elif answers:
                cid_scores[cid] = {"unknown": len(answers)}
        out[item["criterion_id"]] = cid_scores
    return out


def build_report(panel_dir, *, custody=None):
    root, panel = read_panel(panel_dir)
    records = latest_success(read_ledger(root))
    rows = read_ledger(root)
    judges = panel["judges"]
    names = (custody or {}).get("candidates", {})
    results = {}
    blind = _blind(panel, records, names)
    for judge in judges:
        per_criterion = {}
        for item in panel["criteria"]:
            per_criterion[item["criterion_id"]] = _public(
                _tally(panel, records, judge["judge_id"], item["criterion_id"])
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
        pointwise = _pointwise(panel, records, judge["judge_id"])
        results[judge["judge_id"]] = {
            "kind": judge["kind"],
            "model": judge["model"],
            "pointwise": pointwise if any(pointwise.values()) else None,
            "completed_requests": sum(
                1 for r in records.values() if r["judge_id"] == judge["judge_id"]
            ),
            "attempts": sum(1 for r in rows if r["judge_id"] == judge["judge_id"]),
            "usage": usage,
            "criteria": per_criterion,
            "family_blind": {k: _public(v) for k, v in blind[judge["judge_id"]].items()}
            if judge["judge_id"] in blind
            else None,
        }
    agreement = {
        item["criterion_id"]: _agreement(panel, records, judges, item["criterion_id"])
        for item in panel["criteria"]
    }
    rank_agreement = {}
    for judge_id, judge in results.items():
        if not judge["pointwise"]:
            continue
        for criterion_id, scores in judge["pointwise"].items():
            means = {cid: v["mean"] for cid, v in scores.items() if "mean" in v}
            for other_id, other in results.items():
                strength = other["criteria"][criterion_id]["strength"]
                if strength is None:
                    continue
                rank_agreement[
                    f"{judge_id}:pointwise|{other_id}:pairwise|{criterion_id}"
                ] = spearman(means, strength)
    return {
        "format": REPORT_FORMAT,
        "panel_digest": panel["digest"],
        "subject": panel.get("subject", "draft"),
        "family_blind_consensus": _consensus(panel, blind) if blind else None,
        "rank_agreement": rank_agreement,
        "candidates": {cid: names.get(cid) for cid in sorted(panel["candidates"])},
        "planned_requests": len(panel["presentations"]) * len(judges),
        "completed_requests": len(records),
        "judges": results,
        "agreement": agreement,
        "semantic_quality": "model_judged_not_human",
        "billing_reconciled": False,
    }


def build_combined(panel_dirs):
    """Pool family-blind comparisons from several panels, keyed by model name."""
    if len(panel_dirs) < 2:
        raise JudgeError("two_panels_required")
    cases, criteria, judges = [], None, set()
    for panel_dir in panel_dirs:
        root, panel = read_panel(panel_dir)
        try:
            custody = strict_json_loads(
                (root / "custody.json").read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise JudgeError("unreadable_custody") from exc
        if custody.get("panel_digest") != panel["digest"]:
            raise JudgeError("custody_mismatch")
        ids = [item["criterion_id"] for item in panel["criteria"]]
        if criteria is None:
            criteria = ids
        elif ids != criteria:
            raise JudgeError("criteria_mismatch")
        names = custody["candidates"]
        blind = _blind(panel, latest_success(read_ledger(root)), names)
        judges |= set(blind)
        cases.append((panel["digest"], names, blind))
    if len({digest for digest, _, _ in cases}) != len(cases):
        raise JudgeError("duplicate_panel")
    models = sorted({name for _, names, _ in cases for name in names.values()})
    out = {}
    for criterion_id in criteria:
        pooled, per_case = [], {}
        for digest, names, blind in cases:
            comparisons = [
                (names[w], names[loser], weight)
                for tallies in blind.values()
                for w, loser, weight in tallies[criterion_id]["comparisons"]
            ]
            pooled += comparisons
            in_case = sorted(set(names.values()))
            per_case[digest] = (
                bradley_terry(in_case, comparisons) if comparisons else None
            )
        stability = {}
        for (d1, s1), (d2, s2) in combinations(per_case.items(), 2):
            if s1 and s2:
                stability[f"{d1[:12]}|{d2[:12]}"] = spearman(s1, s2)
        out[criterion_id] = {
            "scored_presentations": round(sum(w for _, _, w in pooled)),
            "strength": bradley_terry(models, pooled) if pooled else None,
            "per_case": per_case,
            "case_rank_agreement": stability,
        }
    return {
        "format": "draftbench-judge-combined-v1",
        "panels": [digest for digest, _, _ in cases],
        "judges": sorted(judges),
        "models": models,
        "criteria": out,
        "semantic_quality": "model_judged_not_human",
    }


def render_combined(report):
    criteria = list(report["criteria"])
    lines = [
        "# Family-blind ranking across cases",
        "",
        f"Cases {len(report['panels'])} · judges pooled: "
        + ", ".join(report["judges"])
        + " · model judgments, not human review.",
        "",
        "| Model | " + " | ".join(criteria) + " |",
        "|" + "---|" * (len(criteria) + 1),
    ]
    for model in report["models"]:
        cells = []
        for criterion_id in criteria:
            strength = report["criteria"][criterion_id]["strength"]
            cells.append("n/a" if strength is None else f"{strength[model]:+.2f}")
        lines.append(f"| {model} | " + " | ".join(cells) + " |")
    lines += ["", "## Rank agreement between cases (Spearman)", ""]
    for criterion_id, value in report["criteria"].items():
        for key, rho in value["case_rank_agreement"].items():
            lines.append(f"- {criterion_id} · {key}: {rho}")
    lines.append("")
    return "\n".join(lines)


def write_combined(panel_dirs, output):
    report = build_combined(panel_dirs)
    _create(
        output, json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
    )
    from pathlib import Path

    _create(Path(output).with_suffix(".md"), render_combined(report))
    return report


def _create(path, text):
    import os

    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise JudgeError("output_exists") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def render_markdown(report):
    lines = [
        "# Brief judge report"
        if report["subject"] == "brief"
        else "# Pairwise judge report",
        "",
        f"Panel `{report['panel_digest'][:12]}` · requests {report['completed_requests']}/{report['planned_requests']} · model judgments, not human review.",
        "",
    ]
    names = report["candidates"]
    pairwise = report["judges"] if report["subject"] == "draft" else {}
    for judge_id, judge in pairwise.items():
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
    for judge_id, judge in report["judges"].items():
        if not judge["pointwise"]:
            continue
        lines += [
            f"## {judge_id} pointwise scores (mean over repeats; level 0 to 3)",
            "",
        ]
        criteria = list(judge["pointwise"])
        lines += [
            "| Candidate | " + " | ".join(criteria) + " |",
            "|" + "---|" * (len(criteria) + 1),
        ]
        for cid in sorted(names):
            cells = []
            for criterion_id in criteria:
                v = judge["pointwise"][criterion_id].get(cid)
                if v is None:
                    cells.append("n/a")
                elif "mean" not in v:
                    cells.append("unknown")
                else:
                    cells.append(f"{v['mean']:.2f} ({v['min']:.1f}-{v['max']:.1f})")
            lines.append(f"| {names[cid] or cid} | " + " | ".join(cells) + " |")
        lines.append("")
    if report["rank_agreement"]:
        lines += ["## Rank agreement (Spearman, pointwise vs pairwise)", ""]
        for key, value in report["rank_agreement"].items():
            lines.append(f"- {key}: {value}")
        lines.append("")
    consensus = report.get("family_blind_consensus")
    if consensus:
        criteria = list(consensus["criteria"])
        lines += [
            "## Family-blind consensus (no judge counts on its own model family)",
            "",
            "Judges pooled: " + ", ".join(consensus["judges"]),
            "",
            "| Candidate | " + " | ".join(criteria) + " |",
            "|" + "---|" * (len(criteria) + 1),
        ]
        for cid in sorted(names):
            cells = []
            for criterion_id in criteria:
                strength = consensus["criteria"][criterion_id]["strength"]
                cells.append("n/a" if strength is None else f"{strength[cid]:+.2f}")
            lines.append(f"| {names[cid] or cid} | " + " | ".join(cells) + " |")
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
    from pathlib import Path

    report = build_report(panel_dir, custody=custody)
    _create(
        output, json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
    )
    _create(Path(output).with_suffix(".md"), render_markdown(report))
    return report
