"""Bounded local-only loading. No URL fetching, providers or database access."""

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .identity import strict_json_loads
from .models import Case, FileReference, Suite

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_CASES = 10_000


class SuiteError(ValueError):
    """A fixed, safe error code; never contains file paths or input values."""


@dataclass(frozen=True)
class LoadedSuite:
    manifest: Suite
    cases: tuple[Case, ...]


class _Reader:
    def __init__(self, root: Path):
        self.root = root
        self.total = 0
        self.verified: dict[str, str] = {}

    def read(self, path: Path) -> bytes:
        try:
            if path.is_symlink():
                raise SuiteError("unsafe_reference")
            if not stat.S_ISREG(path.stat().st_mode):
                raise SuiteError("unsafe_reference")
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_BINARY", 0)
            )
            with os.fdopen(os.open(path, flags), "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise SuiteError("unsafe_reference")
                if info.st_size > MAX_FILE_BYTES:
                    raise SuiteError("input_limit")
                data = stream.read(
                    min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - self.total) + 1
                )
            self.total += len(data)
            if len(data) > MAX_FILE_BYTES or self.total > MAX_TOTAL_BYTES:
                raise SuiteError("input_limit")
            return data
        except OSError as exc:
            raise SuiteError("input_unreadable") from exc

    def reference_path(self, reference: FileReference) -> Path:
        candidate = self.root
        try:
            for part in reference.path.split("/"):
                candidate = candidate / part
                if candidate.is_symlink():
                    raise SuiteError("unsafe_reference")
            if not candidate.resolve().is_relative_to(self.root):
                raise SuiteError("unsafe_reference")
            return candidate
        except (OSError, RuntimeError) as exc:
            raise SuiteError("unsafe_reference") from exc

    def reference(self, reference: FileReference, *, retain: bool = False) -> bytes:
        path = self.reference_path(reference)
        previous = self.verified.get(reference.path)
        if previous is not None and previous != reference.sha256:
            raise SuiteError("artifact_hash_mismatch")
        if previous is not None and not retain:
            return b""
        data = self.read(path)
        if hashlib.sha256(data).hexdigest() != reference.sha256:
            raise SuiteError("artifact_hash_mismatch")
        self.verified[reference.path] = reference.sha256
        return data


def _parse(data: bytes, model, code: str):
    try:
        return model.model_validate(strict_json_loads(data.decode("utf-8")))
    except (ValueError, UnicodeError, ValidationError, RecursionError) as exc:
        raise SuiteError(code) from exc


def load_suite(manifest_path: str | Path) -> LoadedSuite:
    """Load a caller-selected manifest; references are relative to its directory.

    Only regular local files are accepted. The input tree must not be modified
    concurrently: containment checks are not an OS sandbox for hostile writers.
    """
    try:
        path = Path(manifest_path).absolute()
        root = path.parent.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise SuiteError("input_unreadable") from exc
    reader = _Reader(root)
    manifest = _parse(reader.read(path), Suite, "invalid_manifest")
    raw_cases = reader.reference(manifest.cases, retain=True)
    lines = raw_cases.splitlines()
    if not lines or len(lines) > MAX_CASES:
        raise SuiteError("case_count_limit")
    cases = []
    seen_ids = set()
    families = {}
    sources = {}
    for line in lines:
        if not line.strip():
            raise SuiteError("invalid_case")
        case = _parse(line, Case, "invalid_case")
        if case.case_id in seen_ids:
            raise SuiteError("duplicate_case_id")
        seen_ids.add(case.case_id)
        if (
            case.source_family in families
            and families[case.source_family] != case.split
        ):
            raise SuiteError("split_family_collision")
        families[case.source_family] = case.split
        artifacts = list(case.evidence.artifacts)
        if case.source.artifact is not None:
            source_hash = case.source.artifact.file.sha256
            if source_hash in sources and sources[source_hash] != case.split:
                raise SuiteError("split_source_collision")
            sources[source_hash] = case.split
            artifacts.append(case.source.artifact)
        if manifest.rights.usage == "redistributable" and any(
            rights.usage != "redistributable"
            for rights in [case.rights, *(a.rights for a in artifacts)]
        ):
            raise SuiteError("rights_conflict")
        for artifact in artifacts:
            reader.reference(artifact.file)
        cases.append(case)
    if manifest.purpose == "synthetic_infrastructure" and any(
        c.split == "confirmation" for c in cases
    ):
        raise SuiteError("synthetic_confirmation")
    return LoadedSuite(manifest=manifest, cases=tuple(cases))
