"""Explicit local projection and operator attestation. Never publishes or hosts."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import Contract, Digest, Identifier, Text
from .reporting import (
    VERSION,
    ReportError,
    identified,
    read_json,
    read_report,
    write_report,
)

PANELS = ("writer", "reviewer", "revision")


class ReleaseSelection(Contract):
    format: Literal["draftbench-release-selection-v1"]
    panels: Annotated[
        list[Literal["writer", "reviewer", "revision"]], Field(max_length=3)
    ]
    evidence_ids: Annotated[list[Identifier], Field(max_length=4096)]

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.panels)) != len(self.panels) or len(
            set(self.evidence_ids)
        ) != len(self.evidence_ids):
            raise ValueError("duplicate_release_selector")
        return self


class ReleaseApproval(Contract):
    format: Literal["draftbench-release-approval-v1"]
    approved: Literal[True]
    source_report_identity: Digest
    projection_digest: Digest
    reviewer_kind: Literal["human", "synthetic"]
    declared_reviewer: Text
    rights_attestation: Text
    selection: ReleaseSelection

    @model_validator(mode="before")
    @classmethod
    def explicit_attestation(cls, value):
        if not isinstance(value, dict) or value.get("approved") is not True:
            raise ValueError("explicit_approval_required")
        if any(
            not isinstance(value.get(key), str) or not value[key].strip()
            for key in ("declared_reviewer", "rights_attestation")
        ):
            raise ValueError("explicit_attestation_required")
        return value


def projection(report, selection):
    selection = ReleaseSelection.model_validate(selection)
    if report["format"] != VERSION:
        raise ReportError("private_report_required")
    evidence = {row["evidence_id"]: row for row in report["evidence"]}
    if set(selection.evidence_ids) - set(evidence):
        raise ReportError("unknown_evidence_selector")
    public_evidence = []
    for key in sorted(selection.evidence_ids):
        row = evidence[key]
        if report["snapshot"].get("provider_run") is not None:
            # Provider manifests deliberately omit input rights, so the source
            # components' redistribution terms are unbound; never infer them.
            raise ReportError("provider_evidence_rights_unbound")
        snapshot = report["snapshot"]["run"]
        rights = [snapshot["rights"], *row["rights"]] if snapshot else []
        if not rights or any(right["usage"] != "redistributable" for right in rights):
            raise ReportError("evidence_rights_not_redistributable")
        # No native IDs, source paths, prompts, annotation notes, or identity maps.
        public_evidence.append(
            {
                "role": row["role"],
                "rights": rights,
                "units": [
                    {"kind": unit["kind"], "content": unit["content"]}
                    for unit in row["units"]
                ],
                "feedback": row["feedback"],
            }
        )
    # Fixed numeric/status panels only. Reference-conditioned nested metrics may
    # contain producer metadata, so they are deliberately not public in v1.
    panels = []
    for panel in report["panels"]:
        if panel["role"] not in selection.panels:
            continue
        panels.append(
            {
                key: panel[key]
                for key in (
                    "configuration",
                    "role",
                    "status",
                    "metric_version",
                    "operation_counts",
                    "coverage",
                    "source_family_count",
                    "quality",
                    "revision_gain",
                    "cost",
                    "latency",
                )
            }
        )
    return identified(
        {
            "format": "draftbench-public-report-v1",
            "purpose": "exploratory_development",
            "watermark": report["watermark"],
            "interpretation": report["interpretation"],
            "panels": panels,
            "evidence": public_evidence,
            "approval_meaning": "Operator attestation only; not authenticated identity, independent review, or hosting permission.",
        }
    )


def check_release(report_dir, selection_path):
    report = read_report(report_dir)
    selected = ReleaseSelection.model_validate(read_json(selection_path))
    public = projection(report, selected.model_dump(mode="json"))
    return {
        "source_report_identity": report["identity"],
        "projection_digest": public["identity"],
        "selection": selected.model_dump(mode="json"),
        "projection": public,
        "approval_required": True,
        "export_performed": False,
    }


def export_release(report_dir, approval_path, output):
    report = read_report(report_dir)
    approval = ReleaseApproval.model_validate(read_json(approval_path))
    public = projection(report, approval.selection.model_dump(mode="json"))
    if (
        approval.source_report_identity != report["identity"]
        or approval.projection_digest != public["identity"]
    ):
        raise ReportError("release_approval_mismatch")
    if approval.reviewer_kind == "synthetic" and not report["watermark"].startswith(
        "SYNTHETIC"
    ):
        raise ReportError("synthetic_approval_requires_synthetic_report")
    result = write_report(public, output)
    return {**result, "local_export_only": True, "hosting_performed": False}
