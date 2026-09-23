"""The v1 canonical JSON profile (not RFC 8785/JCS).

UTF-8, sorted string keys, compact separators, no Unicode normalization, no
non-finite numbers or duplicate keys. Identity omits only the root identity.
File references instead hash the original bytes, including whitespace.
"""

import hashlib
import json
import math

MAX_JSON_DEPTH = 48


def _check(value: object, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("json_depth_limit")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non_finite_json")
        return
    if type(value) is list:
        for item in value:
            _check(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _check(item, depth + 1)
        return
    raise ValueError("non_json_type")


def canonical_bytes(value: object) -> bytes:
    _check(value)
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid_json") from exc


def identity(document: dict) -> str:
    if type(document) is not dict:
        raise ValueError("identity_requires_object")
    return hashlib.sha256(
        canonical_bytes(
            {key: value for key, value in document.items() if key != "identity"}
        )
    ).hexdigest()


def strict_json_loads(text: str) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("non_finite_json")

    try:
        value = json.loads(
            text, object_pairs_hook=pairs, parse_constant=reject_constant
        )
        # Also rejects floating-point overflow (e.g. 1e999) and lone surrogates.
        canonical_bytes(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid_json") from exc
