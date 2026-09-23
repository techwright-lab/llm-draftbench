"""Build only the public synthetic smoke suite from its owned fixture."""

import copy
import hashlib
from pathlib import Path

from draftbench.identity import canonical_bytes, identity, strict_json_loads
from draftbench.loader import load_suite


def main():
    directory = Path(__file__).resolve().parent
    original = directory.parent / "synthetic"
    case = strict_json_loads((original / "cases.jsonl").read_text(encoding="utf-8"))
    assert isinstance(case, dict)
    case["case_id"] = "synthetic-chain-1"
    case["source_family"] = "synthetic-chain-family-1"
    for role in ("reviewer", "revision"):
        case["generator"][role] = copy.deepcopy(case["generator"]["writer"])
        case["generator"][role]["input"]["messages"] = [
            {
                "role": "user",
                "content": f"Exercise the synthetic {role} fixture. No factual assessment.",
            }
        ]
    case["identity"] = identity(case)
    body = canonical_bytes(case) + b"\n"
    (directory / "cases.jsonl").write_bytes(body)
    (directory / "source.txt").write_bytes((original / "source.txt").read_bytes())
    suite = strict_json_loads((original / "suite.json").read_text(encoding="utf-8"))
    assert isinstance(suite, dict)
    suite["suite_id"] = "synthetic-workflow-smoke"
    suite["cases"]["sha256"] = hashlib.sha256(body).hexdigest()
    suite["identity"] = identity(suite)
    manifest = directory / "suite.json"
    manifest.write_bytes(canonical_bytes(suite) + b"\n")
    loaded = load_suite(manifest)
    print(f"Verified {len(loaded.cases)} synthetic smoke case(s).")


if __name__ == "__main__":
    main()
