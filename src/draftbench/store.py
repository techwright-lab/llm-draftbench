"""Bounded, immutable local artifacts addressed by SHA-256 of their exact bytes.

Objects are regular 0600 files named only by lowercase hexadecimal digests.
Publication uses an exclusive private staging file, file fsync, a no-replace
hard link, and directory fsync. Staging debris is never an object or scavenged.

The caller must own and trust the store directory and its ancestors. Directory
file descriptors and no-follow opens limit substitution races; this is not a
sandbox against hostile concurrent modification by the same OS user. Filesystem
support for hard links and directory fsync is required; failures are not silently
replaced by weaker publication. A failed put may already have published a valid
object; retrying the same bytes is safe.
"""

import hashlib
import os
import re
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from .identity import canonical_bytes, strict_json_loads

MAX_OBJECT_BYTES = 8 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ERROR_CODES = frozenset(
    {
        "unsafe_store",
        "store_exists",
        "store_unavailable",
        "store_io_error",
        "invalid_digest",
        "invalid_artifact",
        "artifact_limit",
        "artifact_missing",
        "unsafe_artifact",
        "artifact_hash_mismatch",
        "invalid_json",
    }
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_DIRECTORY = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _NONBLOCK


class StoreError(ValueError):
    """A fixed safe code, never a path, input value, or underlying OS message."""

    def __init__(self, code: str):
        super().__init__(
            code if type(code) is str and code in _ERROR_CODES else "store_io_error"
        )


def _inode(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _check_object(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise StoreError("unsafe_artifact")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise StoreError("unsafe_artifact")
    if info.st_size > MAX_OBJECT_BYTES:
        raise StoreError("artifact_limit")


def _check_digest(digest: str) -> None:
    if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
        raise StoreError("invalid_digest")


class ArtifactStore:
    """Create a new store exclusively, or open an existing ordinary directory.

    Existing directories are never emptied or chmodded. Operations reopen and
    verify the original directory identity; published objects are never rewritten,
    repaired, or unlinked. Only a put's own staging entry is eligible for cleanup.
    """

    def __init__(self, root: Path | str, *, create: bool = False):
        try:
            if not isinstance(root, (Path, str)) or "\x00" in str(root):
                raise StoreError("unsafe_store")
            self._root = Path(root).absolute()
        except (OSError, TypeError, ValueError):
            raise StoreError("unsafe_store") from None
        self._identity: tuple[int, int] | None = None
        if create:
            try:
                os.mkdir(self._root, mode=0o700)
                created = self._root.lstat()
                if not stat.S_ISDIR(created.st_mode):
                    raise StoreError("unsafe_store")
                self._identity = _inode(created)
                if stat.S_IMODE(created.st_mode) != 0o700:
                    # Even a restrictive umask must not make the new root
                    # impossible to open. Never chmod an existing store or link.
                    os.chmod(self._root, 0o700, follow_symlinks=False)
            except FileExistsError:
                raise StoreError("store_exists") from None
            except OSError:
                raise StoreError("store_io_error") from None
        try:
            with self._directory() as directory:
                self._identity = _inode(os.fstat(directory))
                if create:
                    os.fchmod(directory, 0o700)
                    os.fsync(directory)
            if create:
                # Persist the newly created root entry as well as its contents.
                parent = os.open(self._root.parent, _DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        except OSError:
            raise StoreError("store_io_error") from None

    @contextmanager
    def _directory(self):
        try:
            info = self._root.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise StoreError("unsafe_store")
            directory = os.open(self._root, _DIRECTORY)
        except FileNotFoundError:
            raise StoreError("store_unavailable") from None
        except OSError:
            raise StoreError("store_io_error") from None
        try:
            opened = os.fstat(directory)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _inode(opened) != _inode(info)
                or (self._identity is not None and _inode(opened) != self._identity)
            ):
                raise StoreError("unsafe_store")
            yield directory
        finally:
            os.close(directory)

    def _read(
        self,
        directory: int,
        digest: str,
        *,
        missing_ok: bool = False,
        sync: bool = False,
    ) -> bytes | None:
        try:
            info = os.stat(digest, dir_fd=directory, follow_symlinks=False)
            _check_object(info)
            descriptor = os.open(
                digest, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _BINARY, dir_fd=directory
            )
            try:
                opened = os.fstat(descriptor)
                _check_object(opened)
                if _inode(opened) != _inode(info):
                    raise StoreError("unsafe_artifact")
                stream = os.fdopen(descriptor, "rb")
                descriptor = None  # Ownership transferred to the stream.
                with stream:
                    data = stream.read(MAX_OBJECT_BYTES + 1)
                    _check_object(os.fstat(stream.fileno()))
                    if len(data) > MAX_OBJECT_BYTES:
                        raise StoreError("artifact_limit")
                    if (
                        len(data) != opened.st_size
                        or hashlib.sha256(data).hexdigest() != digest
                    ):
                        raise StoreError("artifact_hash_mismatch")
                    if sync:
                        os.fsync(stream.fileno())
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise StoreError("artifact_missing") from None
        return data

    def get(self, digest: str) -> bytes:
        """Read at most 8 MiB, verifying file type, permissions, size and hash."""
        _check_digest(digest)
        try:
            with self._directory() as directory:
                data = self._read(directory, digest)
                # _read only returns None when explicitly passed missing_ok=True.
                assert data is not None
                return data
        except OSError:
            raise StoreError("store_io_error") from None

    def put(self, data: bytes) -> str:
        """Durably publish exact bytes without replacing any existing object."""
        if type(data) is not bytes:
            raise StoreError("invalid_artifact")
        if len(data) > MAX_OBJECT_BYTES:
            raise StoreError("artifact_limit")
        digest = hashlib.sha256(data).hexdigest()
        try:
            with self._directory() as directory:
                existing = self._read(directory, digest, missing_ok=True, sync=True)
                if existing is not None:
                    if existing != data:
                        raise StoreError("artifact_hash_mismatch")
                    # A racing writer may have linked but not yet synced the name.
                    os.fsync(directory)
                else:
                    self._publish(directory, digest, data)
            return digest
        except OSError:
            raise StoreError("store_io_error") from None

    def _publish(self, directory: int, digest: str, data: bytes) -> None:
        name = None
        descriptor = None
        identity = None
        try:
            for _ in range(16):
                candidate = ".staging-" + secrets.token_hex(16)
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY,
                        0o600,
                        dir_fd=directory,
                    )
                except FileExistsError:
                    continue
                name = candidate
                break
            if descriptor is None or name is None:
                raise StoreError("store_io_error")
            identity = _inode(os.fstat(descriptor))
            os.fchmod(descriptor, 0o600)
            stream = os.fdopen(descriptor, "wb")
            descriptor = None
            with stream:
                if stream.write(data) != len(data):
                    raise StoreError("store_io_error")
                stream.flush()
                os.fsync(stream.fileno())
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            _check_object(info)
            if _inode(info) != identity or info.st_size != len(data):
                raise StoreError("unsafe_artifact")
            try:
                os.link(
                    name,
                    digest,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                # No clobber: a winner's object must still pass every read check.
                pass
            if self._read(directory, digest) != data:
                raise StoreError("artifact_hash_mismatch")
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                if name is not None and identity is not None:
                    self._discard_stage(directory, name, identity)
        os.fsync(directory)

    @staticmethod
    def _discard_stage(directory: int, name: str, identity: tuple[int, int]) -> None:
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and _inode(info) == identity:
                os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass

    def put_json(self, value: object) -> str:
        """Publish the shared v1 canonical JSON profile as UTF-8 bytes."""
        try:
            data = canonical_bytes(value)
        except (ValueError, TypeError, RecursionError):
            raise StoreError("invalid_json") from None
        return self.put(data)

    def get_json(self, digest: str) -> object:
        """Verify bytes first, then parse strict JSON without normalizing it."""
        data = self.get(digest)
        try:
            return strict_json_loads(data.decode("utf-8"))
        except (ValueError, RecursionError):
            raise StoreError("invalid_json") from None
