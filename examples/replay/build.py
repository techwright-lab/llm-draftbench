"""Build the public synthetic native-replay suite in the TG export shape."""

import copy
import hashlib
from pathlib import Path

from draftbench.identity import canonical_bytes, identity, strict_json_loads
from draftbench.loader import load_suite

WRITER_SYSTEM = "You are a synthetic SEO writer fixture. Return the structured fields."
REVIEWER_SYSTEM = "You settle named synthetic review items. Quote verbatim."
BODY = "## Synthetic heading\n\nA fabricated fixture body with no factual claims."
ITEMS = ("promise_not_delivered", "unrun_protocol", "self_contradiction")
SIDECAR = b'{"safety":"Synthetic private sidecar canary; never generator input."}\n'


def writer_user(extra=""):
    return (
        "Write the full SEO article based on this brief.\n\n"
        "## Content Brief\n- Title: Synthetic brief\n"
        "- Target Keyword: synthetic fixture\n- Target Word Count: 1200\n"
        "- Content Type: blog_post\n" + extra
    )


def reviewer_user():
    items = "".join(
        f"- item_id: {item}\n  question: Synthetic question for {item}.\n"
        for item in ITEMS
    )
    return (
        "## Site\nSynthetic (synthetic.invalid)\n\n"
        "## The promise (from the brief)\nTitle: Synthetic exported title\n\n"
        "Meta description: Synthetic exported meta description.\n\n"
        "Target keyword: synthetic fixture\nContent intent: not declared\n"
        "Profile: seo\n\n## Sibling drafts\n\nNone.\n\n"
        f"## The draft\n{BODY}\n\n## Items\n{items}\n"
        "Answer every item_id above and only those. Then list factual_issues.\n"
    )


def main():
    directory = Path(__file__).resolve().parent
    original = directory.parent / "synthetic"
    case = strict_json_loads((original / "cases.jsonl").read_text(encoding="utf-8"))
    assert isinstance(case, dict)
    case["case_id"] = "synthetic-replay-1"
    case["source_family"] = "synthetic-replay-family-1"
    case["metadata"]["content_type"]["value"] = "blog_post"
    case["metadata"]["domain"]["value"] = "seo"
    case["metadata"]["lane"]["value"] = "seo"
    case["metadata"]["body_format"]["value"] = "markdown"
    case["source"]["artifact"]["file"] = {
        "path": "source.json",
        "sha256": hashlib.sha256(SIDECAR).hexdigest(),
    }
    draft = copy.deepcopy(case["history"]["drafts"][0])
    draft["units"] = [{"unit_id": "body", "kind": "text", "content": BODY}]
    case["history"]["drafts"] = [draft]
    template = case["generator"]["writer"]["input"]
    template["prompt_version"] = "19370494"
    for role, system, user, drafts in (
        ("writer", WRITER_SYSTEM, writer_user(), []),
        ("reviewer", REVIEWER_SYSTEM, reviewer_user(), [draft["draft_id"]]),
        (
            "revision",
            WRITER_SYSTEM,
            writer_user(f"\n## Previous Draft (bounded)\n\n{BODY}\n"),
            [draft["draft_id"]],
        ),
    ):
        packet = copy.deepcopy(template)
        packet["messages"] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        packet["draft_ids"] = drafts
        case["generator"][role] = {"state": "available", "input": packet}
    case["identity"] = identity(case)
    body = canonical_bytes(case) + b"\n"
    (directory / "cases.jsonl").write_bytes(body)
    (directory / "source.json").write_bytes(SIDECAR)
    suite = strict_json_loads((original / "suite.json").read_text(encoding="utf-8"))
    assert isinstance(suite, dict)
    suite["suite_id"] = "synthetic-native-replay"
    suite["cases"]["sha256"] = hashlib.sha256(body).hexdigest()
    suite["identity"] = identity(suite)
    manifest = directory / "suite.json"
    manifest.write_bytes(canonical_bytes(suite) + b"\n")
    loaded = load_suite(manifest)
    print(f"Verified {len(loaded.cases)} synthetic replay case(s).")


if __name__ == "__main__":
    main()
