"""Explicit POSIX credential file loading; never shell/dotenv/environment lookup."""

import os
import re
import stat
from pathlib import Path

_KEYS = frozenset(
    {
        "DRAFTBENCH_OPENAI_API_KEY",
        "DRAFTBENCH_OPENAI_ORGANIZATION",
        "DRAFTBENCH_OPENAI_PROJECT",
        "DRAFTBENCH_ANTHROPIC_API_KEY",
    }
)
_TOKEN = re.compile(r"[A-Za-z0-9_.:/+@=-]{1,4096}\Z")


def load_credentials(path):
    """Return only allowlisted literal tokens, never retain/echo parse diagnostics.

    Reject symlinks in every path component, non-owner files, hard links, anything
    except mode 0600, and changes during reading. Same-user hostile writers are
    outside the trust boundary. No quotes, exports, expansion or multiline values.
    """
    try:
        path = Path(path).absolute()
        if os.name != "posix" or any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1
                or before.st_size > 32768
            ):
                raise ValueError
            raw = stream.read(32769)
            after = os.fstat(stream.fileno())

            def signature(s):
                return (
                    s.st_dev,
                    s.st_ino,
                    s.st_mode,
                    s.st_uid,
                    s.st_nlink,
                    s.st_size,
                    s.st_mtime_ns,
                    s.st_ctime_ns,
                )

            if signature(before) != signature(after) or len(raw) > 32768:
                raise ValueError
        values = {}
        for line in raw.decode("ascii").split("\n"):
            line = line.strip(" \t\r")
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if (
                not sep
                or key not in _KEYS
                or key in values
                or not _TOKEN.fullmatch(value)
            ):
                raise ValueError
            if any(
                word in value.lower()
                for word in (
                    "replace",
                    "dummy",
                    "placeholder",
                    "example",
                    "changeme",
                    "fixture",
                )
            ):
                raise ValueError
            values[key] = value
        if values.keys() != _KEYS:
            raise ValueError
        if not re.fullmatch(
            r"org_[A-Za-z0-9_-]+", values["DRAFTBENCH_OPENAI_ORGANIZATION"]
        ):
            raise ValueError
        if not re.fullmatch(
            r"proj_[A-Za-z0-9_-]+", values["DRAFTBENCH_OPENAI_PROJECT"]
        ):
            raise ValueError
        return values
    except Exception:
        raise ValueError("invalid_explicit_credentials") from None
