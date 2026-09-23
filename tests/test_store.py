"""Immutable, bounded artifact storage with real process/crash coverage."""

import hashlib
import os
import selectors
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import draftbench.store as store_module
from draftbench.identity import canonical_bytes
from draftbench.store import ArtifactStore, StoreError

LIMIT = 8 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def seed(root, data, *, name=None, mode=0o600):
    path = root / (name if name is not None else digest(data))
    path.write_bytes(data)
    path.chmod(mode)
    return path


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "objects", create=True)


def test_create_is_exclusive_private_and_open_preserves_contents(tmp_path):
    root = tmp_path / "objects"
    store = ArtifactStore(str(root), create=True)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    key = store.put(b"original")
    before = (root / key).stat()
    with pytest.raises(StoreError, match="^store_exists$"):
        ArtifactStore(root, create=True)
    assert ArtifactStore(root).get(key) == b"original"
    assert (root / key).stat().st_ino == before.st_ino


@pytest.mark.parametrize("mask", [0, 0o077, 0o777])
def test_new_store_and_objects_have_exact_private_modes_under_umask(tmp_path, mask):
    root = tmp_path / "objects"
    previous = os.umask(mask)
    try:
        store = ArtifactStore(root, create=True)
        key = store.put(b"private")
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / key).stat().st_mode) == 0o600
        assert store.get(key) == b"private"
    finally:
        os.umask(previous)
        # Leave this test's own root traversable even when a RED run exposes
        # a mode-000 creation bug. Never touch another test run's directories.
        if root.is_dir() and not root.is_symlink():
            root.chmod(0o700)


def test_existing_ordinary_directory_is_not_chmodded(tmp_path):
    root = tmp_path / "objects"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    store = ArtifactStore(root)
    assert store.get(store.put(b"data")) == b"data"
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


@pytest.mark.parametrize("create", [False, True])
@pytest.mark.parametrize("kind", ["file", "symlink", "dangling", "fifo"])
def test_unsafe_roots_are_rejected_without_touching_targets(tmp_path, create, kind):
    root = tmp_path / "objects"
    target = tmp_path / "target"
    target.mkdir()
    marker = seed(target, b"untouched")
    if kind == "file":
        root.write_bytes(b"untouched")
    elif kind == "symlink":
        root.symlink_to(target, target_is_directory=True)
    elif kind == "dangling":
        root.symlink_to(tmp_path / "missing", target_is_directory=True)
    else:
        os.mkfifo(root)
    with pytest.raises(StoreError) as caught:
        ArtifactStore(root, create=create)
    assert str(caught.value) == ("store_exists" if create else "unsafe_store")
    assert marker.read_bytes() == b"untouched"
    assert root.lstat()


def test_open_missing_and_create_missing_parent_do_not_create_ancestors(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(StoreError, match="^store_unavailable$"):
        ArtifactStore(missing)
    with pytest.raises(StoreError, match="^store_io_error$"):
        ArtifactStore(missing / "nested", create=True)
    assert not missing.exists()


@pytest.mark.parametrize("root", [None, 42, b"objects", "bad\x00root"])
def test_invalid_root_inputs_have_safe_errors(root):
    with pytest.raises(StoreError, match="^unsafe_store$"):
        ArtifactStore(root)


@pytest.mark.parametrize("operation", ["get", "put", "get_json", "put_json"])
def test_root_symlink_replacement_is_rejected(tmp_path, operation):
    root = tmp_path / "objects"
    store = ArtifactStore(root, create=True)
    key = store.put_json({"content": "frozen"})
    saved = tmp_path / "saved"
    root.rename(saved)
    root.symlink_to(saved, target_is_directory=True)
    argument = key if operation.startswith("get") else b"new"
    if operation == "put_json":
        argument = {"content": "new"}
    with pytest.raises(StoreError, match="^unsafe_store$"):
        getattr(store, operation)(argument)
    assert ArtifactStore(saved).get_json(key) == {"content": "frozen"}


def test_regular_root_replacement_is_detected(tmp_path):
    root = tmp_path / "objects"
    store = ArtifactStore(root, create=True)
    root.rename(tmp_path / "old")
    root.mkdir()
    with pytest.raises(StoreError, match="^unsafe_store$"):
        store.put(b"new")
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("data", [b"", b"\x00\xff\x80\n", b"raw bytes\n"])
def test_raw_roundtrip_uses_exact_digest_filename_and_private_mode(
    store, tmp_path, data
):
    key = store.put(data)
    path = tmp_path / "objects" / key
    assert key == digest(data)
    assert path.read_bytes() == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.get(key) == data
    assert {p.name for p in path.parent.iterdir()} == {key}


def test_idempotence_verifies_without_rewriting_or_linking(
    store, tmp_path, monkeypatch
):
    data = b"same object"
    key = store.put(data)
    path = tmp_path / "objects" / key
    before = path.stat()

    def forbidden(*args, **kwargs):
        raise AssertionError("an existing object must not be rewritten")

    monkeypatch.setattr(store_module.os, "link", forbidden)
    assert store.put(data) == key
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_mode) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_ctime_ns,
        before.st_mode,
    )


def test_reuses_independently_seeded_correct_private_object(store, tmp_path):
    path = seed(tmp_path / "objects", b"preexisting")
    before = path.stat()
    assert store.put(b"preexisting") == path.name
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("operation", ["get", "put"])
def test_tampering_is_rejected_and_never_overwritten(store, tmp_path, operation):
    original = b"original"
    key = store.put(original)
    path = tmp_path / "objects" / key
    path.write_bytes(b"tampered")
    with pytest.raises(StoreError, match="^artifact_hash_mismatch$"):
        getattr(store, operation)(key if operation == "get" else original)
    assert path.read_bytes() == b"tampered"
    assert {p.name for p in path.parent.iterdir()} == {key}


@pytest.mark.parametrize("mode", [0o400, 0o644, 0o660, 0o666, 0o700, 0o4600])
@pytest.mark.parametrize("operation", ["get", "put"])
def test_unsafe_object_modes_are_rejected_not_repaired(
    store, tmp_path, mode, operation
):
    path = seed(tmp_path / "objects", b"data", mode=mode)
    with pytest.raises(StoreError, match="^unsafe_artifact$"):
        getattr(store, operation)(path.name if operation == "get" else b"data")
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert path.read_bytes() == b"data"


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo"])
@pytest.mark.parametrize("operation", ["get", "put"])
def test_unsafe_objects_are_rejected_without_following_or_blocking(
    store, tmp_path, kind, operation
):
    data = b"data"
    key = digest(data)
    root = tmp_path / "objects"
    path = root / key
    target = seed(tmp_path, data)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "dangling":
        path.symlink_to(tmp_path / "absent")
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path, 0o600)
    with pytest.raises(StoreError, match="^unsafe_artifact$"):
        getattr(store, operation)(key if operation == "get" else data)
    assert target.read_bytes() == data
    assert path.lstat()
    assert {p.name for p in root.iterdir()} == {key}


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_object_replacement_during_open_is_nofollow_and_nonblocking(
    store, tmp_path, monkeypatch, kind
):
    root = tmp_path / "objects"
    data = b"original"
    key = store.put(data)
    path = root / key
    target = seed(tmp_path, data)
    original_open = os.open

    def opening(candidate, flags, *args, **kwargs):
        if candidate == key:
            assert kwargs["dir_fd"] is not None
            assert flags & os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                assert flags & os.O_NOFOLLOW
            path.unlink()
            if kind == "symlink":
                path.symlink_to(target)
            else:
                os.mkfifo(path, mode=0o600)
        return original_open(candidate, flags, *args, **kwargs)

    monkeypatch.setattr(store_module.os, "open", opening)
    with pytest.raises(StoreError) as caught:
        store.get(key)
    assert str(caught.value) in {"unsafe_artifact", "store_io_error"}
    assert target.read_bytes() == data
    assert path.lstat()


def test_idempotence_compares_actual_bytes_even_if_hashes_collide(
    store, tmp_path, monkeypatch
):
    key = store.put(b"original")
    path = tmp_path / "objects" / key

    class Collision:
        def hexdigest(self):
            return key

    monkeypatch.setattr(store_module.hashlib, "sha256", lambda data: Collision())
    with pytest.raises(StoreError, match="^artifact_hash_mismatch$"):
        store.put(b"different")
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "../escape",
        "/escape",
        "a" * 64 + ".json",
        "a" * 64 + "\n",
        " " + "a" * 64,
        "a" * 64 + "\x00",
        "a" * 32 + "/" + "a" * 31,
        "a" * 32 + "\\" + "a" * 31,
        "\uff41" * 64,
        b"a" * 64,
        None,
        42,
        Path("objects"),
    ],
)
@pytest.mark.parametrize("method", ["get", "get_json"])
def test_digests_are_strict_not_paths(store, key, method):
    with pytest.raises(StoreError, match="^invalid_digest$"):
        getattr(store, method)(key)


def test_missing_digest_is_not_a_path_disclosure(store):
    with pytest.raises(StoreError, match="^artifact_missing$") as caught:
        store.get("0" * 64)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "data", ["text", None, 1, bytearray(b"data"), memoryview(b"data")]
)
def test_put_requires_bytes(store, data):
    with pytest.raises(StoreError, match="^invalid_artifact$"):
        store.put(data)


def test_object_byte_limit_on_writes_and_reads(store, tmp_path):
    exact = b"x" * LIMIT
    key = store.put(exact)
    assert store.get(key) == exact
    with pytest.raises(StoreError, match="^artifact_limit$"):
        store.put(exact + b"x")
    path = tmp_path / "objects" / key
    with path.open("r+b") as stream:
        stream.truncate(LIMIT + 1)
    with pytest.raises(StoreError, match="^artifact_limit$"):
        store.get(key)
    with pytest.raises(StoreError, match="^artifact_limit$"):
        store.put(exact)
    assert {p.name for p in path.parent.iterdir()} == {key}


def test_read_is_bounded_even_when_size_metadata_is_stale(store, monkeypatch):
    data = b"x" * 64
    key = store.put(data)
    monkeypatch.setattr(store_module, "MAX_OBJECT_BYTES", 32)
    real_fstat = os.fstat
    real_stat = os.stat

    def smaller(info):
        if stat.S_ISREG(info.st_mode):
            values = list(info)
            values[6] = 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(store_module.os, "fstat", lambda fd: smaller(real_fstat(fd)))
    monkeypatch.setattr(
        store_module.os, "stat", lambda *a, **kw: smaller(real_stat(*a, **kw))
    )
    with pytest.raises(StoreError, match="^artifact_limit$"):
        store.get(key)


def test_json_is_canonical_utf8_and_does_not_normalize_unicode(store):
    value = {"z": [None, True, 1.25], "a": "caf\u00e9 \u2603 \U0001f680"}
    key = store.put_json(value)
    assert store.get(key) == canonical_bytes(value)
    assert store.get_json(key) == value
    assert store.put_json(dict(reversed(list(value.items())))) == key
    assert store.put_json("\u00e9") != store.put_json("e\u0301")


@pytest.mark.parametrize("value", [None, False, 42, 1.25, "", [], {}])
def test_json_scalar_and_container_roundtrips(store, value):
    assert store.get_json(store.put_json(value)) == value


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"x":{"k":1,"k":2}}',
        b'{"x":1,"\\u0078":2}',
        b'{"x":NaN}',
        b"Infinity",
        b"-Infinity",
        b"1e999",
        b'"\\ud800"',
        b'{"\\udfff":1}',
        b'"\xff"',
        b'"\xed\xa0\x80"',
        b"{} trailing",
        b"",
        b"\xef\xbb\xbf{}",
        b"[" * 60 + b"0" + b"]" * 60,
    ],
)
def test_strict_json_rejects_duplicate_nonfinite_unicode_and_depth(store, raw):
    key = store.put(raw)
    assert store.get(key) == raw
    with pytest.raises(StoreError, match="^invalid_json$") as caught:
        store.get_json(key)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        "\ud800",
        {"\udfff": 1},
        {1: 2},
        (1,),
        b"x",
    ],
)
def test_invalid_json_values_do_not_create_objects(store, tmp_path, value):
    with pytest.raises(StoreError, match="^invalid_json$"):
        store.put_json(value)
    assert list((tmp_path / "objects").iterdir()) == []


def test_json_cycles_and_serialized_size_are_bounded(store, tmp_path):
    cycle = []
    cycle.append(cycle)
    with pytest.raises(StoreError, match="^invalid_json$"):
        store.put_json(cycle)
    value = "x" * (LIMIT - 2)
    key = store.put_json(value)
    assert len(store.get(key)) == LIMIT
    with pytest.raises(StoreError, match="^artifact_limit$"):
        store.put_json(value + "x")
    assert {p.name for p in (tmp_path / "objects").iterdir()} == {key}


def test_staging_and_unrelated_debris_do_not_invalidate_reads(store, tmp_path):
    root = tmp_path / "objects"
    key = store.put_json({"frozen": True})
    partial = seed(root, b'{"partial":', name=".staging-abandoned")
    unrelated = seed(root, b"unrelated", name="notes.txt")
    (root / ".staging-symlink").symlink_to(tmp_path / "absent")
    os.mkfifo(root / ".staging-fifo")
    assert store.get_json(key) == {"frozen": True}
    assert store.put_json({"frozen": True}) == key
    assert store.get(store.put(b"another")) == b"another"
    assert partial.read_bytes() == b'{"partial":'
    assert unrelated.read_bytes() == b"unrelated"
    with pytest.raises(StoreError, match="^invalid_digest$"):
        store.get(partial.name)


def test_publication_flushes_and_fsyncs_before_link_then_syncs_directory(
    store, tmp_path, monkeypatch
):
    data = b"durable data"
    key = digest(data)
    root = tmp_path / "objects"
    events = []
    real_fsync, real_link, real_unlink = os.fsync, os.link, os.unlink

    def fsync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            assert not (root / key).exists()
            staged = list(root.iterdir())
            assert len(staged) == 1
            assert staged[0].read_bytes() == data
            assert stat.S_IMODE(staged[0].stat().st_mode) == 0o600
            events.append("file_fsync")
        else:
            assert (root / key).read_bytes() == data
            assert {p.name for p in root.iterdir()} == {key}
            events.append("directory_fsync")
        return real_fsync(fd)

    def link(src, dst, **kwargs):
        assert events == ["file_fsync"]
        assert dst == key
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
        assert kwargs["follow_symlinks"] is False
        assert (root / src).read_bytes() == data
        events.append("link")
        return real_link(src, dst, **kwargs)

    def unlink(path, **kwargs):
        assert str(path).startswith(".staging-")
        events.append("unlink_stage")
        return real_unlink(path, **kwargs)

    monkeypatch.setattr(store_module.os, "fsync", fsync)
    monkeypatch.setattr(store_module.os, "link", link)
    monkeypatch.setattr(store_module.os, "unlink", unlink)
    assert store.put(data) == key
    assert events == ["file_fsync", "link", "unlink_stage", "directory_fsync"]


def test_idempotent_put_fsyncs_existing_file_before_directory(store, monkeypatch):
    key = store.put(b"durable")
    events = []
    real_fsync = os.fsync

    def fsync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real_fsync(fd)

    monkeypatch.setattr(store_module.os, "fsync", fsync)
    assert store.put(b"durable") == key
    assert events == ["file", "directory"]


@pytest.mark.parametrize(
    "phase", ["open", "chmod", "write", "flush", "file_fsync", "link"]
)
def test_failure_before_publish_cleans_only_owned_stage(
    store, tmp_path, monkeypatch, phase
):
    root = tmp_path / "objects"
    keep = store.put(b"keep")
    unrelated = seed(root, b"abandoned", name=".staging-unrelated")
    original = {
        name: getattr(os, name)
        for name in ("open", "fchmod", "fdopen", "fsync", "link")
    }

    def fail(*args, **kwargs):
        raise OSError("SENSITIVE_MARKER input value and filesystem location")

    def opening(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            return fail()
        return original["open"](path, flags, *args, **kwargs)

    class FaultyWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            if phase == "write":
                self.stream.write(data[:2])
                fail()
            return self.stream.write(data)

        def flush(self):
            if phase == "flush":
                fail()
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

    def fdopen(fd, mode):
        stream = original["fdopen"](fd, mode)
        return FaultyWriter(stream) if mode == "wb" else stream

    target, replacement = {
        "open": ("open", opening),
        "chmod": ("fchmod", fail),
        "write": ("fdopen", fdopen),
        "flush": ("fdopen", fdopen),
        "file_fsync": ("fsync", fail),
        "link": ("link", fail),
    }[phase]
    with monkeypatch.context() as patch:
        patch.setattr(store_module.os, target, replacement)
        with pytest.raises(StoreError, match="^store_io_error$") as caught:
            store.put(b"new publication")
        assert caught.value.__suppress_context__
        assert caught.value.__cause__ is None
        assert "SENSITIVE_MARKER" not in str(caught.value)
    assert {p.name for p in root.iterdir()} == {keep, unrelated.name}
    assert store.get(keep) == b"keep"
    assert unrelated.read_bytes() == b"abandoned"
    assert store.get(store.put(b"new publication")) == b"new publication"


@pytest.mark.parametrize("phase", ["unlink", "directory_fsync"])
def test_failure_after_publish_keeps_verified_object_and_retry_is_safe(
    store, tmp_path, monkeypatch, phase
):
    root = tmp_path / "objects"
    real_fsync = os.fsync
    data = b"published before failure"

    def fail_unlink(*args, **kwargs):
        raise OSError("SENSITIVE_MARKER")

    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("SENSITIVE_MARKER")
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        if phase == "unlink":
            patch.setattr(store_module.os, "unlink", fail_unlink)
        else:
            patch.setattr(store_module.os, "fsync", fail_directory_fsync)
        with pytest.raises(StoreError, match="^store_io_error$"):
            store.put(data)
    path = root / digest(data)
    before = path.stat()
    assert store.get(path.name) == data
    assert store.put(data) == path.name
    assert path.stat().st_ino == before.st_ino
    assert path.stat().st_mtime_ns == before.st_mtime_ns


def test_exclusive_staging_collision_never_removes_existing_file(
    store, tmp_path, monkeypatch
):
    token = "0" * 32
    unrelated = seed(tmp_path / "objects", b"unrelated", name=".staging-" + token)
    monkeypatch.setattr(store_module.secrets, "token_hex", lambda length: token)
    with pytest.raises(StoreError, match="^store_io_error$"):
        store.put(b"new")
    assert unrelated.read_bytes() == b"unrelated"
    assert list(unrelated.parent.iterdir()) == [unrelated]


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_cleanup_does_not_unlink_a_replaced_staging_name(
    store, tmp_path, monkeypatch, replacement
):
    root = tmp_path / "objects"
    preserved = seed(tmp_path, b"unrelated")
    staged = []
    real_unlink = os.unlink

    def fail_link(src, dst, **kwargs):
        path = root / src
        staged.append(path)
        # Rename keeps the original inode allocated, so inode reuse cannot hide
        # replacement of the staging entry.
        path.rename(root / ".original-stage")
        if replacement == "file":
            path.write_bytes(b"replacement")
        else:
            path.symlink_to(preserved)
        raise OSError("publication failed")

    def unlink(path, **kwargs):
        assert path not in [p.name for p in staged]
        return real_unlink(path, **kwargs)

    monkeypatch.setattr(store_module.os, "link", fail_link)
    monkeypatch.setattr(store_module.os, "unlink", unlink)
    with pytest.raises(StoreError, match="^store_io_error$"):
        store.put(b"new")
    assert staged[0].lstat()
    assert preserved.read_bytes() == b"unrelated"
    assert not (root / digest(b"new")).exists()


def test_error_constructor_cannot_echo_untrusted_values():
    error = StoreError("SENSITIVE_MARKER")
    assert isinstance(error, ValueError)
    assert str(error) == "store_io_error"


# Child interpreters do not inherit pytest monkeypatches: deny networking there
# explicitly as well. No multiprocessing manager or socket-backed IPC is used.
CHILD = r"""
import socket

def denied(*args, **kwargs):
    raise AssertionError("network access forbidden in offline tests")

socket.socket = socket.create_connection = socket.getaddrinfo = denied

import sys
import draftbench.store as module
from draftbench.store import ArtifactStore, StoreError

root, payload, when = sys.argv[1:]
data = payload.encode("utf-8")
original_link = module.os.link

def link(*args, **kwargs):
    if when == "after":
        result = original_link(*args, **kwargs)
    print("ready", flush=True)
    sys.stdin.readline()
    if when == "before":
        result = original_link(*args, **kwargs)
    return result

module.os.link = link
try:
    store = ArtifactStore(root)
    key = store.put(data)
    assert store.get(key) == data
    print(key, flush=True)
except StoreError as exc:
    print(str(exc), flush=True)
    sys.exit(7)
"""


@contextmanager
def writers(root, payloads, when="before"):
    processes = []
    try:
        for payload in payloads:
            process = subprocess.Popen(
                [sys.executable, "-c", CHILD, str(root), payload, when],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(process)
        for process in processes:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=15), "writer did not reach publication"
            assert process.stdout.readline().strip() == "ready"
        yield processes
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=15)


def release(processes):
    # Release every writer before waiting for any one of them.
    for process in processes:
        process.stdin.write("\n")
        process.stdin.flush()
    return [process.communicate(timeout=15) for process in processes]


@pytest.mark.parametrize("same_payload", [True, False])
def test_real_racing_writers_publish_without_clobbering(tmp_path, same_payload):
    root = tmp_path / "objects"
    store = ArtifactStore(root, create=True)
    payloads = ["shared" if same_payload else f"payload-{i}" for i in range(4)]
    keys = {digest(p.encode()) for p in payloads}
    with writers(root, payloads) as processes:
        assert not any((root / key).exists() for key in keys)
        assert len(list(root.glob(".staging-*"))) == len(processes)
        outputs = release(processes)
        for process, payload, (stdout, stderr) in zip(processes, payloads, outputs):
            assert process.returncode == 0, stderr
            assert stdout.strip() == digest(payload.encode())
    assert {p.name for p in root.iterdir()} == keys
    for payload in payloads:
        assert store.get(digest(payload.encode())) == payload.encode()
    for path in root.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.stat().st_nlink == 1


def test_racing_conflicting_object_is_never_replaced_or_unlinked(tmp_path):
    root = tmp_path / "objects"
    store = ArtifactStore(root, create=True)
    payload = "raced data"
    key = digest(payload.encode())
    with writers(root, [payload] * 3) as processes:
        conflict = seed(root, b"corruption", name=key)
        before = conflict.stat()
        outputs = release(processes)
        for process, (stdout, stderr) in zip(processes, outputs):
            assert process.returncode == 7, stderr
            assert stdout.strip() == "artifact_hash_mismatch"
    assert conflict.stat().st_ino == before.st_ino
    assert conflict.read_bytes() == b"corruption"
    assert {p.name for p in root.iterdir()} == {key}
    with pytest.raises(StoreError, match="^artifact_hash_mismatch$"):
        store.get(key)


@pytest.mark.parametrize("when", ["before", "after"])
def test_killed_writer_debris_is_ignored_and_published_objects_remain_readable(
    tmp_path, when
):
    root = tmp_path / "objects"
    store = ArtifactStore(root, create=True)
    keep = store.put_json({"frozen": True})
    payload = "interrupted writer"
    key = digest(payload.encode())
    with writers(root, [payload], when=when) as processes:
        processes[0].kill()
        processes[0].communicate(timeout=15)
    abandoned = list(root.glob(".staging-*"))
    assert len(abandoned) == 1
    original_stage = abandoned[0].read_bytes()
    reopened = ArtifactStore(root)
    assert reopened.get_json(keep) == {"frozen": True}
    if when == "before":
        assert not (root / key).exists()
        with pytest.raises(StoreError, match="^artifact_missing$"):
            reopened.get(key)
    else:
        assert reopened.get(key) == payload.encode()
    assert reopened.put(payload.encode()) == key
    assert reopened.get(key) == payload.encode()
    assert abandoned[0].read_bytes() == original_stage
    assert {p.name for p in root.iterdir()} == {keep, key, abandoned[0].name}
