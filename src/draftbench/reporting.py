"""Read-only offline report snapshots. No adapter dispatch or recovery lives here."""

import csv
import hashlib
import html
import io
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from .annotation import _normalize, _session, _write_new
from .annotation_models import AdjudicationAnswer, AnnotationRubric, Answer
from .identity import canonical_bytes, identity, strict_json_loads
from .ledger import Ledger
from .loader import _Reader
from .scoring.models import ScoringBundle, artifact_digest, reference_scope
from .scoring.reporting import load_scoring_bundle, score_bundle
from .store import ArtifactStore
from .workflow import _directory, _manifest, _run_lock, _verify_artifacts

VERSION = "draftbench-report-v1"
RUBRIC = {
    "rubric_id": "mechanical-only",
    "version": "1",
    "checks": [
        {"check_id": "nonempty", "kind": "nonempty", "applicability": "required"}
    ],
}
NOTICE = "Exploratory development only. MECHANICAL ONLY checks are not semantic quality, a leaderboard, or release permission."


class ReportError(ValueError):
    """Safe fixed report failure code."""


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def identified(value):
    value = {**value, "schema_version": "1"}
    return {**value, "identity": identity(value)}


def read_json(path):
    path = Path(path).absolute()
    value = strict_json_loads(_Reader(path.parent.resolve()).read(path).decode("utf-8"))
    if not isinstance(value, dict):
        raise ReportError("report_object_required")
    return value


def snapshot_run(directory):
    """Verify the frozen run; never recover result_saved or uncertain attempts."""
    root = _directory(directory)
    with _run_lock(root):
        store = ArtifactStore(root / "objects")
        with Ledger.open(root / "ledger.sqlite3", readonly=True) as ledger:
            manifest = _manifest(root, store, ledger)
            cases = {case.case_id: case for case in manifest.cases}
            _verify_artifacts(ledger, store, cases)
            work = []
            from .workflow import native_receipt

            for plan in ledger.plan():
                state = ledger.state(plan["work_id"])
                case = cases[plan["case_id"]]
                # Conservatively retain every component's rights, not code licensing.
                rights = []

                def collect(value):
                    if isinstance(value, dict):
                        if "rights" in value:
                            rights.append(value["rights"])
                        for child in value.values():
                            collect(child)
                    elif isinstance(value, list):
                        for child in value:
                            collect(child)

                collect(case.model_dump(mode="json"))
                work.append(
                    {
                        **plan,
                        **state,
                        **(
                            {"native_failure": native_receipt(ledger, store, state)}
                            if manifest.adapter == "inspect-fixture"
                            and state["state"] in ("failed", "limited")
                            else {}
                        ),
                        "case_identity": case.identity,
                        "source_family": case.source_family,
                        "rights": rights,
                        "result": store.get_json(state["result_digest"])
                        if state["state"] == "completed"
                        else None,
                    }
                )
            return {
                "manifest_digest": ledger.manifest_digest,
                "rights": manifest.suite.rights.model_dump(mode="json"),
                "summary": ledger.summary(),
                "work": work,
            }


def bundle_from_run(snapshot):
    cases, bindings = [], {}
    for row in snapshot["work"]:
        if row["state"] != "completed" or row["role"] == "reviewer":
            continue
        units = row["result"]["units"]
        artifact = {"units": units, "sha256": artifact_digest(units)}
        binding = {
            "artifact_sha256": artifact["sha256"],
            "rubric_id": RUBRIC["rubric_id"],
            "rubric_version": "1",
        }
        case = identified(
            {
                "case_id": row["work_id"],
                "source_family": row["source_family"],
                "artifact": artifact,
                "rubric": RUBRIC,
                "sources_state": "unavailable",
                "sources": [],
                "reference": {
                    "binding": binding,
                    "producer": None,
                    "basis": None,
                    "independence": "unknown",
                    "acceptability": "unknown",
                    "defect_inventory": "unavailable",
                    "defects": [],
                },
                "review": None,
                "alignment": None,
            }
        )
        cases.append(case)
        bindings[row["work_id"]] = {
            key: row[key]
            for key in (
                "work_id",
                "case_identity",
                "request_digest",
                "result_digest",
                "role",
            )
        }
        bindings[row["work_id"]]["artifact_sha256"] = artifact["sha256"]
        bindings[row["work_id"]]["scoring_case_identity"] = case["identity"]
    if not cases:
        raise ReportError("no_completed_scoring_artifacts")
    bundle = ScoringBundle.model_validate(
        identified(
            {
                "format": "draftbench-scoring-v1",
                "purpose": snapshot.get("purpose", "synthetic_infrastructure"),
                "rights": snapshot["rights"],
                "cases": cases,
            }
        )
    )
    return bundle, bindings


def prepare_report(run_dir, output):
    snapshot = snapshot_run(run_dir)
    bundle, bindings = bundle_from_run(snapshot)
    rubric = AnnotationRubric.model_validate(
        identified(
            {
                "format": "draftbench-annotation-rubric-v1",
                "rubric_id": "mechanical-only",
                "rubric_version": "1",
                "instructions": "SYNTHETIC infrastructure exercise. Assess only the stated mechanical condition; do not infer semantic quality.",
                "criteria": [
                    {
                        "criterion_id": "nonempty",
                        "question": "Does every supplied unit contain non-whitespace text?",
                    }
                ],
            }
        )
    )
    root = _directory(output, create=True)
    for name, value in {
        "bundle.json": bundle.model_dump(mode="json"),
        "bindings.json": {
            "format": "draftbench-run-bindings-v1",
            "run_snapshot_digest": digest(snapshot),
            "bundle_identity": bundle.identity,
            "cases": bindings,
        },
        "rubric.json": rubric.model_dump(mode="json"),
    }.items():
        _write_new(root / name, canonical_bytes(value))
    return {"prepared_cases": len(bundle.cases), "bundle_identity": bundle.identity}


def select_annotations(bundle, session, selection):
    """Explicit record choice, exact frozen case identity, re-normalized raw response."""
    if (
        not isinstance(selection, dict)
        or set(selection) != {"format", "records"}
        or selection["format"] != "draftbench-annotation-selection-v1"
    ):
        raise ReportError("invalid_annotation_selection")
    chosen = selection["records"]
    if not isinstance(chosen, dict) or set(chosen) - {
        case.case_id for case in bundle.cases
    }:
        raise ReportError("unknown_annotation_selection")
    with _session(session, readonly=True) as context:
        _, _, frozen, rubric, raters, packets, mappings, store = context
        source = {case.case_id: case for case in frozen.cases}
        records = {record["record_id"]: record for record in store.records()}
        cases, details = [], []
        for case in bundle.cases:
            if case.case_id not in chosen:
                cases.append(case.model_dump(mode="json"))
                continue
            if source.get(case.case_id) != case:
                raise ReportError("annotation_source_mismatch")
            record = records.get(chosen[case.case_id])
            if record is None:
                raise ReportError("unknown_annotation_record")
            body = record["body"]
            if body["case_identity"] != case.identity:
                raise ReportError("annotation_source_mismatch")
            assignment, packet = packets[body["packet_id"]]
            task = next(
                (task for task in packet.tasks if task.task_id == record["task_id"]),
                None,
            )
            if task is None:
                raise ReportError("annotation_source_mismatch")
            mapping, frozen_case = mappings[task.task_id]
            answer = (
                AdjudicationAnswer if record["kind"] == "adjudication" else Answer
            ).model_validate(body["original_response"])
            if (
                answer.response_id != record["record_id"]
                or answer.status == "unanswered"
            ):
                raise ReportError("annotation_record_mismatch")
            normalized = _normalize(
                answer,
                task,
                mapping,
                frozen_case,
                rubric,
                raters[assignment.rater_key],
                SimpleNamespace(
                    session_id=packet.session_id,
                    packet_id=packet.packet_id,
                    rater_id=packet.rater_id,
                ),
                synthetic=frozen.purpose == "synthetic_infrastructure",
            )
            if normalized != body:
                raise ReportError("annotation_record_mismatch")
            originals = [
                row
                for row in records.values()
                if row["kind"] == "original"
                and row["body"]["case_identity"] == case.identity
            ]
            if record["kind"] == "adjudication" and set(body["based_on"]) != {
                row["record_id"] for row in originals
            }:
                raise ReportError("stale_adjudication")
            value = case.model_dump(mode="json")
            complete = body["coverage"]["complete"]
            value["reference"].update(
                producer=body["producer"],
                basis=body["basis"],
                independence=body["independence"],
                acceptability=(
                    "acceptable" if body["decision"] == "acceptable" else "unacceptable"
                )
                if complete
                else "unknown",
                defect_inventory=body["defect_inventory"]
                if complete
                else "unavailable",
                defects=body["defects"] if complete else [],
            )
            value["alignment"] = (
                None  # old semantic judgments NEVER rebind by finding ID
            )
            value = identified(value)
            cases.append(value)
            details.append(
                {
                    "case_id": case.case_id,
                    "record_id": record["record_id"],
                    "condition": "explicit_rater_reference"
                    if record["kind"] == "original"
                    else "explicit_adjudication",
                    "complete": complete,
                    "original_count": len(originals),
                    "disagreement": len(
                        {
                            digest(
                                {
                                    key: row["body"][key]
                                    for key in ("decision", "criteria", "defects")
                                }
                            )
                            for row in originals
                        }
                    )
                    > 1,
                }
            )
        value = bundle.model_dump(mode="json")
        value["cases"] = cases
        return ScoringBundle.model_validate(identified(value)), details


def build_report(
    *,
    run_dir=None,
    provider_run_dir=None,
    bundle_path=None,
    bindings_path=None,
    session=None,
    selection_path=None,
):
    if run_dir is not None and provider_run_dir is not None:
        raise ReportError("ambiguous_report_run")
    if bindings_path is not None and (
        bundle_path is None or (run_dir is None and provider_run_dir is None)
    ):
        raise ReportError("bindings_require_run_and_bundle")
    snapshot = snapshot_run(run_dir) if run_dir is not None else None
    bundle = load_scoring_bundle(bundle_path) if bundle_path is not None else None
    bindings = read_json(bindings_path) if bindings_path is not None else None
    provider = None
    if provider_run_dir is not None:
        from .provider_reporting import provider_bundle, snapshot_provider

        if bundle is None or bindings is None:
            raise ReportError("provider_preparation_required")
        provider = snapshot_provider(provider_run_dir)
        expected, expected_bindings = provider_bundle(
            provider, bundle.rights.model_dump(mode="json")
        )
        if bundle != expected or bindings != expected_bindings:
            raise ReportError("provider_scoring_binding_mismatch")
    if snapshot and bundle:
        expected, expected_bindings = bundle_from_run(snapshot)
        bindings = read_json(bindings_path) if bindings_path else None
        if (
            bindings
            != {
                "format": "draftbench-run-bindings-v1",
                "run_snapshot_digest": digest(snapshot),
                "bundle_identity": expected.identity,
                "cases": expected_bindings,
            }
            or bundle != expected
        ):
            raise ReportError("run_scoring_binding_mismatch")
    if not snapshot and not bundle:
        raise ReportError("report_input_required")
    if bool(session) != bool(selection_path) or (session and bundle is None):
        raise ReportError("annotation_selection_required")
    original = bundle.model_dump(mode="json") if bundle else None
    details = []
    if session:
        bundle, details = select_annotations(bundle, session, read_json(selection_path))
    frozen = {
        "run": snapshot,
        "source_bundle": original,
        "bindings": bindings,
        "scoring_bundle": bundle.model_dump(mode="json") if bundle else None,
        "annotation_selection": details,
    }
    if provider is not None:
        frozen["provider_run"] = provider
    return normalize_report(frozen)


def _validate_frozen(frozen):
    from .adapters.fake import FakeResult
    from .ledger import STATES

    snapshot = frozen["run"]
    original = (
        ScoringBundle.model_validate(frozen["source_bundle"])
        if frozen["source_bundle"]
        else None
    )
    scored = (
        ScoringBundle.model_validate(frozen["scoring_bundle"])
        if frozen["scoring_bundle"]
        else None
    )
    if snapshot:
        rows = snapshot["work"]
        counts = dict.fromkeys(STATES, 0)
        if len({row["work_id"] for row in rows}) != len(rows):
            raise ReportError("report_snapshot_mismatch")
        for row in rows:
            counts[row["state"]] += 1
            if row["state"] == "completed":
                from .adapters.inspect import InspectResult

                result_type = (
                    InspectResult
                    if row["result"].get("adapter") == "inspect-fixture"
                    else FakeResult
                )
                result = result_type.model_validate(row["result"])
                if (
                    digest(row["result"]) != row["result_digest"]
                    or result.request_digest != row["request_digest"]
                    or result.role != row["role"]
                ):
                    raise ReportError("report_snapshot_mismatch")
            elif row["result"] is not None:
                raise ReportError("report_snapshot_mismatch")
        if (
            counts != snapshot["summary"]["states"]
            or len(rows) != snapshot["summary"]["work_count"]
        ):
            raise ReportError("report_snapshot_mismatch")
        if original:
            expected, bindings = bundle_from_run(snapshot)
            if original != expected or frozen["bindings"] != {
                "format": "draftbench-run-bindings-v1",
                "run_snapshot_digest": digest(snapshot),
                "bundle_identity": expected.identity,
                "cases": bindings,
            }:
                raise ReportError("run_scoring_binding_mismatch")
    provider = frozen.get("provider_run")
    if provider is not None:
        from .provider_reporting import provider_bundle

        if snapshot is not None or original is None:
            raise ReportError("provider_preparation_required")
        expected, bindings = provider_bundle(
            provider, original.rights.model_dump(mode="json")
        )
        if original != expected or frozen["bindings"] != bindings:
            raise ReportError("provider_scoring_binding_mismatch")
    elif snapshot is None and frozen["bindings"] is not None:
        raise ReportError("bindings_require_run_and_bundle")
    if bool(original) != bool(scored):
        raise ReportError("report_snapshot_mismatch")
    if original and scored:
        selected = {item["case_id"] for item in frozen["annotation_selection"]}
        if len(selected) != len(frozen["annotation_selection"]) or selected - {
            case.case_id for case in original.cases
        }:
            raise ReportError("report_snapshot_mismatch")
        if len(original.cases) != len(scored.cases):
            raise ReportError("report_snapshot_mismatch")
        for before, after in zip(original.cases, scored.cases):
            if before.case_id not in selected:
                if before != after:
                    raise ReportError("report_snapshot_mismatch")
            else:
                a, b = before.model_dump(mode="json"), after.model_dump(mode="json")
                for key in ("identity", "reference", "alignment"):
                    a.pop(key)
                    b.pop(key)
                if a != b or after.alignment is not None:
                    raise ReportError("report_snapshot_mismatch")


def normalize_report(frozen):
    _validate_frozen(frozen)
    snapshot = frozen["run"]
    bundle = (
        ScoringBundle.model_validate(frozen["scoring_bundle"])
        if frozen["scoring_bundle"]
        else None
    )
    cases = {case.case_id: case for case in bundle.cases} if bundle else {}
    work = snapshot["work"] if snapshot else []
    provider = frozen.get("provider_run")
    if provider:
        work = [
            {
                **row,
                **provider["manifest"]["case_metadata"][row["case_id"]],
                "rights": [bundle.rights.model_dump(mode="json")],
                "result": {
                    "adapter": row["result"]["adapter"],
                    "units": [
                        {
                            "unit_id": row["work_id"],
                            "kind": "text",
                            "content": row["result"]["output"],
                        }
                    ],
                    "feedback": None,
                }
                if row["state"] == "completed"
                else None,
            }
            for row in provider["work"]
        ]
    panels = []
    for role in ("writer", "reviewer", "revision"):
        rows = [row for row in work if row["role"] == role]
        selected = [cases[row["work_id"]] for row in rows if row["work_id"] in cases]
        panel = {
            "configuration": (
                next(
                    (
                        row["result"]["adapter"] + "-v1"
                        for row in rows
                        if row["result"] is not None
                    ),
                    "not_observed",
                )
                if work
                else "not_run"
            ),
            "role": role,
            "status": "not_run"
            if not rows or all(row["state"] == "planned" for row in rows)
            else "incomplete"
            if any(row["state"] != "completed" for row in rows)
            else "unassessed"
            if not selected
            else "inconclusive",
            "metric_version": VERSION,
            "operation_counts": dict(
                sorted(Counter(row["state"] for row in rows).items())
            ),
            "coverage": {"numerator": len(selected), "denominator": len(rows)},
            "source_family_count": len({row["source_family"] for row in rows}),
            "quality": "not_assessed",
            "revision_gain": "not_assessed",
            "cost": "not_assessed",
            "latency": "not_assessed",
            "scoring": None,
        }
        if selected:
            value = bundle.model_dump(mode="json")
            value["cases"] = [case.model_dump(mode="json") for case in selected]
            panel["scoring"] = score_bundle(
                ScoringBundle.model_validate(identified(value))
            )
            if any(
                cohort["deterministic"]["outcomes"]["invalid"]
                for cohort in panel["scoring"]["cohorts"]
            ):
                panel["status"] = "invalid"
        panels.append(panel)
    evidence = []
    for row in work:
        if row["result"] is not None:
            evidence.append(
                {
                    "evidence_id": row["work_id"],
                    "role": row["role"],
                    "source_family": row["source_family"],
                    "request_digest": row["request_digest"],
                    "result_digest": row["result_digest"],
                    "rights": row["rights"],
                    "units": row["result"]["units"],
                    "feedback": row["result"]["feedback"],
                }
            )
    if bundle and not work:
        for case in bundle.cases:
            evidence.append(
                {
                    "evidence_id": case.case_id,
                    "role": "supplied_artifact",
                    "source_family": case.source_family,
                    "request_digest": None,
                    "result_digest": None,
                    "rights": [bundle.rights.model_dump(mode="json")],
                    "units": [
                        unit.model_dump(mode="json") for unit in case.artifact.units
                    ],
                    "feedback": None,
                }
            )
    from .scoring.deterministic import score_deterministic

    for item in evidence:
        case = cases.get(item["evidence_id"])
        item["deterministic"] = score_deterministic(case) if case else None
        item["reference_scope"] = (
            reference_scope(case.reference) if case else "unassessed"
        )
    return identified(
        {
            "format": VERSION,
            "purpose": "exploratory_development",
            "custody": "verified_provider_preparation"
            if frozen.get("provider_run")
            else "verified_run"
            if snapshot
            else "standalone",
            "provenance": frozen["provider_run"]["manifest"]["provenance"]
            if frozen.get("provider_run")
            else "synthetic"
            if snapshot
            else "supplied_artifact",
            "model_execution_performed": (
                True
                if frozen["provider_run"]["manifest"]["provenance"] == "provider"
                else False
            )
            if frozen.get("provider_run")
            else False
            if snapshot
            else None,
            "watermark": (
                "SYNTHETIC — NOT MODEL MEASUREMENTS"
                if frozen["provider_run"]["manifest"]["provenance"] == "fixture"
                else "SAVED PROVIDER OUTPUTS — MECHANICAL ONLY"
            )
            if frozen.get("provider_run")
            else "SYNTHETIC — NOT MODEL MEASUREMENTS"
            if snapshot or (bundle and bundle.purpose == "synthetic_infrastructure")
            else "OFFLINE SUPPLIED REFERENCES — NOT RUN",
            "interpretation": NOTICE,
            "execution_status": "not_run"
            if not work or all(row["state"] == "planned" for row in work)
            else "complete"
            if all(row["state"] == "completed" for row in work)
            else "incomplete",
            "panels": panels,
            "scoring_only": score_bundle(bundle) if bundle and not work else None,
            "annotation_choices": [
                {key: value for key, value in item.items() if key != "record_id"}
                for item in frozen["annotation_selection"]
            ],
            "annotation_coverage": {
                "numerator": sum(
                    item["complete"] for item in frozen["annotation_selection"]
                ),
                "denominator": len(cases),
            },
            "reference_conditions": [
                {
                    "producer_kind": case.reference.producer.kind
                    if case.reference.producer
                    else None,
                    "scope": reference_scope(case.reference),
                    "basis": case.reference.basis,
                    "independence": case.reference.independence,
                }
                for case in cases.values()
            ],
            "evidence": evidence,
            "snapshot": frozen,
        }
    )


def csv_cell(value):
    text = str(value)
    # Prefix any whitespace/control-leading string, not just obvious formulas.
    if text and (
        text[0].isspace()
        or ord(text[0]) < 32
        or text.lstrip().startswith(("=", "+", "-", "@"))
    ):
        return "'" + text
    return text


def flattened(value, prefix=""):
    if isinstance(value, dict):
        for key in sorted(value):
            child = value[key]
            yield from flattened(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from flattened(child, f"{prefix}[{index}]")
    else:
        yield prefix, value


def output_files(report):
    """One projection drives JSON, CSV and HTML. Text never becomes a URL."""
    visible = {key: value for key, value in report.items() if key != "snapshot"}
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["field", "value"])
    writer.writerows(
        [[csv_cell(key), csv_cell(value)] for key, value in flattened(visible)]
    )
    rows = "".join(
        f"<tr><th scope='row'>{html.escape(key)}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in flattened(visible)
    )
    links, files = [], {}
    for index, evidence in enumerate(report["evidence"]):
        name = f"evidence-{index:05d}.json"
        files[name] = canonical_bytes(evidence)
        links.append(f'<li><a href="{name}">Evidence {index + 1}</a></li>')
    panels = "".join(
        '<section class="panel"><h2>'
        + html.escape(panel["role"].title())
        + "</h2><p><strong>"
        + html.escape(panel["configuration"])
        + "</strong> · "
        + html.escape(panel["status"])
        + "</p><p>Scored outputs: "
        + html.escape(str(panel["coverage"]["numerator"]))
        + " / "
        + html.escape(str(panel["coverage"]["denominator"]))
        + " planned roles<br>Source families: "
        + html.escape(str(panel["source_family_count"]))
        + "</p><p>Quality, spend and latency: not assessed.</p></section>"
        for panel in report["panels"]
    )
    page = (
        """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; script-src 'none'">
<title>Offline experiment report</title><style>
.panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem;margin:2rem 0}.panel{padding:1rem;border:1px solid #ccc;background:white}.panel h2{margin-top:0}
body{font:16px/1.6 system-ui,sans-serif;color:#222;background:#faf9f6;margin:0 auto;padding:2rem;max-width:76rem}h1{font-size:2rem;line-height:1.2}header{border-bottom:3px solid #444;padding-bottom:1rem}.notice{font-weight:700}table{border-collapse:collapse;width:100%;font-size:.9rem;table-layout:fixed}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #ccc;padding:.65rem;overflow-wrap:anywhere}th{width:48%;font-weight:500}a{color:#234c66}caption{text-align:left;font-size:1.3rem;font-weight:700;padding:1rem 0}@media(max-width:600px){body{padding:1rem}th{width:40%}}@media print{body{background:white;padding:0}tr{break-inside:avoid}a{color:inherit}}
</style><header><p>OFFLINE LAB / DEVELOPMENT EVIDENCE</p><h1>"""
        + html.escape(report["watermark"])
        + """</h1><p class="notice">"""
        + html.escape(report["interpretation"])
        + """</p></header><main><div class="panels">"""
        + panels
        + """</div><h2>Local evidence</h2><ul>"""
        + "".join(links)
        + """</ul><table><caption>Configuration panels, coverage and evidence</caption><tbody>"""
        + rows
        + "</tbody></table></main></html>"
    )
    return {
        **files,
        "report.json": canonical_bytes(report),
        "report.csv": stream.getvalue().encode(),
        "index.html": page.encode(),
    }


def write_report(report, output):
    files = output_files(report)
    if (
        any(len(data) > 8 * 1024 * 1024 for data in files.values())
        or sum(map(len, files.values())) > 64 * 1024 * 1024
    ):
        raise ReportError("report_limit")
    root = _directory(output, create=True)
    for name, data in files.items():
        _write_new(root / name, data)
    _write_new(
        root / "checksums.json",
        canonical_bytes(
            {
                "format": "draftbench-report-checksums-v1",
                "report_identity": report["identity"],
                "files": {
                    name: hashlib.sha256(data).hexdigest()
                    for name, data in files.items()
                },
            }
        ),
    )
    read_report(root)
    return {"report_identity": report["identity"], "file_count": len(files) + 1}


def read_report(directory):
    root = _directory(directory)
    report = read_json(root / "report.json")
    if identity(report) != report["identity"]:
        raise ReportError("report_identity_mismatch")
    if (
        report.get("format") == VERSION
        and normalize_report(report["snapshot"]) != report
    ):
        raise ReportError("report_snapshot_mismatch")
    files = output_files(report)
    expected = {
        "format": "draftbench-report-checksums-v1",
        "report_identity": report["identity"],
        "files": {
            name: hashlib.sha256(data).hexdigest() for name, data in files.items()
        },
    }
    if read_json(root / "checksums.json") != expected:
        raise ReportError("report_checksum_mismatch")
    reader = _Reader(root)
    for name, data in files.items():
        if reader.read(root / name) != data:
            raise ReportError("report_checksum_mismatch")
    if {path.name for path in root.iterdir()} != {*files, "checksums.json"}:
        raise ReportError("unexpected_report_files")
    return report
