"""Reseal only this public fabricated fixture; never imports external data."""

import hashlib
from pathlib import Path

from draftbench.identity import canonical_bytes, identity, strict_json_loads
from draftbench.loader import load_suite


def main():
    directory = Path(__file__).resolve().parent
    case_path = directory / "cases.jsonl"
    cases = []
    for line in case_path.read_text(encoding="utf-8").splitlines():
        case = strict_json_loads(line)
        artifacts = list(case["evidence"]["artifacts"])
        if case["source"]["artifact"] is not None:
            artifacts.append(case["source"]["artifact"])
        for artifact in artifacts:
            reference = artifact["file"]
            # This helper is scoped to the fixture, not an untrusted importer.
            reference["sha256"] = hashlib.sha256(
                (directory / reference["path"]).read_bytes()
            ).hexdigest()
        case["identity"] = identity(case)
        cases.append(canonical_bytes(case) + b"\n")
    body = b"".join(cases)
    case_path.write_bytes(body)
    path = directory / "suite.json"
    suite = strict_json_loads(path.read_text(encoding="utf-8"))
    suite["cases"]["sha256"] = hashlib.sha256(body).hexdigest()
    suite["identity"] = identity(suite)
    path.write_bytes(canonical_bytes(suite) + b"\n")
    loaded = load_suite(path)
    print(f"Verified {len(loaded.cases)} synthetic fixture case(s).")


if __name__ == "__main__":
    main()
