"""Private offline execution of a fixed synthetic writer/reviewer/revision chain."""

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Callable, Literal

from pydantic import Field, ValidationError, model_validator

from .adapters.fake import AdapterLimited, FakeAdapter, FakeResult
from .adapters.inspect import (
    InspectFixtureAdapter,
    InspectOutcomeError,
    InspectPolicy,
    InspectResult,
)
from .identity import canonical_bytes, strict_json_loads
from .ledger import Ledger
from .loader import LoadedSuite, _Reader, load_suite
from .models import Case, Contract, Digest, FileReference, Suite
from .report import ROLES, generator_payload
from .store import ArtifactStore

PROTOCOL = "draftbench-synthetic-run-v1"
Checkpoint = Callable[[str, dict], None]


class RunError(ValueError):
    """Fixed safe code, without private input or filesystem details."""


class RunPointer(Contract):
    format: Literal["draftbench-synthetic-run-v1"]
    manifest_digest: Digest


class RunManifest(Contract):
    format: Literal["draftbench-synthetic-run-v1"]
    adapter_version: Literal["1"]
    adapter: Literal["fake", "inspect-fixture"] = "fake"
    inspect_policy: InspectPolicy | None = None
    suite: Suite
    cases: Annotated[list[Case], Field(min_length=1, max_length=10_000)]
    files: Annotated[list[FileReference], Field(min_length=1, max_length=30_001)]

    @model_validator(mode="after")
    def adapter_policy(self):
        if (self.adapter == "inspect-fixture") != (self.inspect_policy is not None):
            raise ValueError("invalid_adapter_policy")
        return self


def _digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _validate_synthetic(loaded: LoadedSuite) -> None:
    if loaded.manifest.purpose != "synthetic_infrastructure":
        raise RunError("synthetic_suite_required")
    for case in loaded.cases:
        if case.split != "development":
            raise RunError("synthetic_suite_required")
        for role in ROLES:
            packet = getattr(case.generator, role).input
            if packet is not None and packet.basis != "synthetic":
                raise RunError("synthetic_inputs_required")


def _plan(cases) -> list[dict]:
    work = []
    for case in cases:
        ids = {
            role: _digest(
                {"protocol": PROTOCOL, "case_identity": case.identity, "role": role}
            )
            for role in ROLES
        }
        for role in ROLES:
            dependencies = [] if role == "writer" else [ids["writer"]]
            if role == "revision":
                dependencies.append(ids["reviewer"])
            work.append(
                {
                    "work_id": ids[role],
                    "case_id": case.case_id,
                    "role": role,
                    "dependencies": dependencies,
                }
            )
    return work


def _directory(value, *, create=False) -> Path:
    try:
        root = Path(value).absolute()
        if root.is_symlink():
            raise RunError("unsafe_run_directory")
        parent = root.parent.resolve(strict=True)
        root = parent / root.name
        for ancestor in (root, *root.parents):
            marker = ancestor / ".git"
            if marker.exists() or marker.is_symlink():
                raise RunError("run_inside_git")
        if create:
            root.mkdir(mode=0o700)
            root.chmod(0o700)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise RunError("unsafe_run_directory")
        return root
    except FileExistsError as exc:
        raise RunError("run_exists") from exc
    except (OSError, ValueError) as exc:
        if isinstance(exc, RunError):
            raise
        raise RunError("run_directory_unavailable") from exc


def _fsync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _run_lock(root: Path, *, create=False):
    if os.name != "posix":
        raise RunError("run_platform_unsupported")
    import fcntl

    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(root / "run.lock", flags, 0o600)
    except OSError as exc:
        raise RunError("unsafe_run_lock") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise RunError("unsafe_run_lock")
        if create:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            _fsync(root)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunError("run_busy") from exc
        yield
    finally:
        os.close(descriptor)


def _snapshot(
    path: Path,
    loaded: LoadedSuite,
    store: ArtifactStore,
    *,
    adapter="fake",
    inspect_policy=None,
) -> RunManifest:
    reader = _Reader(path.parent.resolve())
    references = [loaded.manifest.cases]
    for case in loaded.cases:
        if case.source.artifact is not None:
            references.append(case.source.artifact.file)
        references.extend(item.file for item in case.evidence.artifacts)
    unique = {}
    for reference in references:
        if reference.path in unique:
            if unique[reference.path] != reference:
                raise RunError("conflicting_snapshot_reference")
            continue
        data = reader.reference(reference, retain=True)
        if store.put(data) != reference.sha256:
            raise RunError("snapshot_hash_mismatch")
        unique[reference.path] = reference
    return RunManifest(
        format=PROTOCOL,
        adapter_version="1",
        adapter=adapter,
        inspect_policy=inspect_policy,
        suite=loaded.manifest,
        cases=list(loaded.cases),
        files=list(unique.values()),
    )


def _manifest(root: Path, store: ArtifactStore, ledger: Ledger) -> RunManifest:
    try:
        pointer = RunPointer.model_validate(
            strict_json_loads(_Reader(root).read(root / "run.json").decode("utf-8"))
        )
        if pointer.manifest_digest != ledger.manifest_digest:
            raise RunError("run_identity_mismatch")
        manifest = RunManifest.model_validate(store.get_json(pointer.manifest_digest))
        _validate_synthetic(LoadedSuite(manifest.suite, tuple(manifest.cases)))
        references = {item.path: item for item in manifest.files}
        if len(references) != len(manifest.files):
            raise RunError("invalid_run_manifest")
        expected = [manifest.suite.cases]
        for case in manifest.cases:
            expected.extend(item.file for item in case.evidence.artifacts)
            if case.source.artifact is not None:
                expected.append(case.source.artifact.file)
        if references != {item.path: item for item in expected}:
            raise RunError("invalid_run_manifest")
        for item in manifest.files:
            store.get(item.sha256)
        raw_cases = store.get(manifest.suite.cases.sha256).splitlines()
        parsed = [
            Case.model_validate(strict_json_loads(line.decode("utf-8")))
            for line in raw_cases
        ]
        if parsed != manifest.cases or len({case.case_id for case in parsed}) != len(
            parsed
        ):
            raise RunError("invalid_run_manifest")
        if ledger.plan() != _plan(manifest.cases):
            raise RunError("run_plan_mismatch")
        return manifest
    except (ValidationError, UnicodeError, TypeError, KeyError) as exc:
        raise RunError("invalid_run_manifest") from exc


def _request(
    work: dict, case: Case, ledger: Ledger, store: ArtifactStore, manifest=None
) -> dict:
    parents, digests = {}, {}
    for dependency, label in zip(work["dependencies"], ("draft", "review")):
        state = ledger.state(dependency)
        if state["state"] != "completed":
            raise RunError("dependency_unresolved")
        digests[label] = state["result_digest"]
        parents[label] = store.get_json(state["result_digest"])
    manifest = manifest or RunManifest.model_validate(
        store.get_json(ledger.manifest_digest)
    )
    extra = {}
    if manifest.adapter == "inspect-fixture":
        if manifest.inspect_policy is None:
            raise RunError("missing_inspect_policy")
        extra = {"inspect_policy": manifest.inspect_policy.model_dump(mode="json")}
    return {
        **extra,
        "schema_version": "1",
        "adapter": manifest.adapter,
        "adapter_version": "1",
        "role": work["role"],
        "work_id": work["work_id"],
        "case_identity": case.identity,
        "input": generator_payload(case, work["role"]),
        "parent_digests": digests,
        "parents": parents,
    }


def _validated_result(value, request: dict) -> dict:
    model = InspectResult if request["adapter"] == "inspect-fixture" else FakeResult
    result = model.model_validate(value)
    if (
        result.role != request["role"]
        or result.request_digest != _digest(request)
        or result.parent_digests != request["parent_digests"]
    ):
        raise RunError("invalid_adapter_output")
    return result.model_dump(mode="json")


def _verify_artifacts(ledger: Ledger, store: ArtifactStore, cases: dict) -> None:
    manifest = RunManifest.model_validate(store.get_json(ledger.manifest_digest))
    for work in ledger.plan():
        state = ledger.state(work["work_id"])
        if state["request_digest"] is not None:
            request = _request(work, cases[work["case_id"]], ledger, store, manifest)
            if store.get_json(state["request_digest"]) != request or state[
                "request_digest"
            ] != _digest(request):
                raise RunError("request_identity_mismatch")
            if state["result_digest"] is not None:
                _validated_result(store.get_json(state["result_digest"]), request)
            if request["adapter"] == "inspect-fixture" and state["state"] in (
                "failed",
                "limited",
            ):
                native_receipt(ledger, store, state)


def _checkpoint(hook, name, work):
    if hook is not None:
        hook(name, {"work_id": work["work_id"], "role": work["role"]})


def _summary(ledger: Ledger) -> dict:
    summary = ledger.summary()
    known_invocation = any(
        summary["states"][state]
        for state in ("result_saved", "completed", "failed", "limited")
    )
    unknown_invocation = any(
        summary["states"][state] for state in ("in_flight", "uncertain")
    )
    return {
        "schema_version": "1",
        "report_kind": "synthetic_execution_summary",
        "synthetic_execution_performed": True
        if known_invocation
        else None
        if unknown_invocation
        else False,
        "model_execution_performed": False,
        "adapter": "fake",
        "adapter_version": "1",
        "work_count": summary["work_count"],
        "attempt_count": summary["attempt_count"],
        "states": summary["states"],
        "complete": summary["states"]["completed"] == summary["work_count"],
    }


def _drive(ledger, store, manifest, *, max_steps, checkpoint):
    cases = {case.case_id: case for case in manifest.cases}
    _verify_artifacts(ledger, store, cases)
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
        case = cases[work["case_id"]]
        packet = getattr(case.generator, work["role"])
        if state["state"] == "planned" and packet.input is None:
            ledger.skip(
                work["work_id"], state="unavailable", code=f"input_{packet.state}"
            )
            continue
        if any(
            ledger.state(dependency)["state"] != "completed"
            for dependency in work["dependencies"]
        ):
            ledger.skip(work["work_id"], state="blocked", code="dependency_unresolved")
            continue
        # Construct before admission: Inspect's SDK preflight must refuse a
        # missing dependency without reserving or stranding an attempt.
        adapter = (
            InspectFixtureAdapter(manifest.inspect_policy)
            if manifest.adapter == "inspect-fixture"
            else FakeAdapter()
        )
        request = _request(work, case, ledger, store, manifest)
        request_digest = store.put_json(request)
        if state["state"] == "planned":
            try:
                attempt = ledger.reserve(
                    work["work_id"], request_digest, policy=manifest.inspect_policy
                )
            except ValueError as exc:
                if str(exc) != "admission_exhausted":
                    raise
                break
        else:
            attempt = state["attempt_id"]
            if state["request_digest"] != request_digest:
                raise RunError("request_identity_mismatch")
        _checkpoint(checkpoint, "reserved", work)
        ledger.start(attempt)
        _checkpoint(checkpoint, "in_flight", work)
        steps += 1
        try:
            value = adapter.invoke(request)
        except InspectOutcomeError as exc:
            # Durable native receipt survives terminal failure without changing old ledgers.
            _inspect_receipt(ledger, store, attempt, request_digest, exc.native)
            ledger.fail(
                attempt,
                "adapter_limited"
                if exc.native["status"] == "limited"
                else "adapter_failed",
                limited=exc.native["status"] == "limited",
            )
            continue
        except AdapterLimited:
            ledger.fail(attempt, "adapter_limited", limited=True)
            continue
        except Exception:
            if manifest.adapter == "inspect-fixture":
                ledger.recover()
                continue
            ledger.fail(attempt, "adapter_failed")
            continue
        try:
            result = _validated_result(value, request)
        except (ValueError, TypeError):
            if manifest.adapter == "inspect-fixture":
                native = value.get("inspect") if isinstance(value, dict) else None
                _inspect_receipt(
                    ledger,
                    store,
                    attempt,
                    request_digest,
                    {
                        **(native if isinstance(native, dict) else {}),
                        "status": "invalid",
                        "error": "invalid_adapter_output",
                    },
                )
            ledger.fail(attempt, "invalid_adapter_output")
            continue
        digest = store.put_json(result)
        _checkpoint(checkpoint, "artifact_saved", work)
        ledger.record_result(attempt, digest)
        _checkpoint(checkpoint, "result_saved", work)
        ledger.complete(attempt)
        _checkpoint(checkpoint, "completed", work)
    summary = _summary(ledger)
    summary["adapter"] = manifest.adapter
    if manifest.inspect_policy is not None:
        policy = manifest.inspect_policy
        summary["admission"] = {
            "reserved_requests": summary["attempt_count"],
            "reserved_token_units": summary["attempt_count"] * policy.reserved_tokens,
            "reservation_released": False,
            "concurrency": 1,
            "charge_status": "unknown"
            if (summary["states"]["uncertain"] or summary["states"]["failed"])
            else "not_applicable_fixture",
        }
    return summary


def _inspect_receipt(ledger, store, attempt, request_digest, native):
    _write_receipt(
        ledger._path.parent,
        attempt,
        {
            "attempt_id": attempt,
            "request_digest": request_digest,
            "native_digest": store.put_json(native),
        },
    )


def _write_receipt(root, attempt, receipt):
    descriptor = os.open(
        root / f"attempt-{attempt}.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical_bytes(receipt))
        stream.flush()
        os.fsync(stream.fileno())
    _fsync(root)


def native_receipt(ledger, store, state):
    root = ledger._path.parent
    path = root / f"attempt-{state['attempt_id']}.json"
    data = strict_json_loads(_Reader(root).read(path).decode("utf-8"))
    if (
        set(data) != {"attempt_id", "request_digest", "native_digest"}
        or data["attempt_id"] != state["attempt_id"]
        or data["request_digest"] != state["request_digest"]
    ):
        raise RunError("invalid_native_receipt")
    return store.get_json(data["native_digest"])


def _steps(value):
    if value is not None and (type(value) is not int or not 0 <= value <= 30_000):
        raise RunError("invalid_step_limit")


def run_synthetic(
    manifest_path,
    run_dir,
    *,
    max_steps=None,
    checkpoint: Checkpoint | None = None,
    adapter="fake",
    inspect_policy=None,
) -> dict:
    _steps(max_steps)
    if adapter not in ("fake", "inspect-fixture"):
        raise RunError("live_execution_disabled")
    if adapter == "inspect-fixture":
        inspect_policy = InspectPolicy.model_validate(inspect_policy or {})
        InspectFixtureAdapter(inspect_policy)
    elif inspect_policy is not None:
        raise RunError("invalid_adapter_policy")
    loaded = load_suite(manifest_path)
    _validate_synthetic(loaded)
    root = _directory(run_dir, create=True)
    with _run_lock(root, create=True):
        store = ArtifactStore(root / "objects", create=True)
        manifest = _snapshot(
            Path(manifest_path).absolute(),
            loaded,
            store,
            adapter=adapter,
            inspect_policy=inspect_policy,
        )
        digest = store.put_json(manifest.model_dump(mode="json"))
        pointer = RunPointer(format=PROTOCOL, manifest_digest=digest)
        descriptor = os.open(
            root / "run.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(canonical_bytes(pointer.model_dump(mode="json")))
            stream.flush()
            os.fsync(stream.fileno())
        with Ledger.create(
            root / "ledger.sqlite3", _plan(manifest.cases), digest
        ) as ledger:
            _fsync(root)
            _manifest(root, store, ledger)
            return _drive(
                ledger, store, manifest, max_steps=max_steps, checkpoint=checkpoint
            )


def resume_synthetic(
    run_dir, *, max_steps=None, checkpoint: Checkpoint | None = None
) -> dict:
    _steps(max_steps)
    root = _directory(run_dir)
    with _run_lock(root):
        store = ArtifactStore(root / "objects")
        with Ledger.open(root / "ledger.sqlite3") as ledger:
            manifest = _manifest(root, store, ledger)
            return _drive(
                ledger, store, manifest, max_steps=max_steps, checkpoint=checkpoint
            )
