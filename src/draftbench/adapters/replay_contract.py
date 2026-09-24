"""Pure TrustGrowth native-replay contract: messages, schemas and substitution.

Schemas are pinned from TrustGrowth `SeoContentSchema` and `ReviewLedgerSchema`
rendered by Schematist at TG_REVISION. The export carries messages only.
"""

import hashlib
import re

from ..identity import canonical_bytes, strict_json_loads

TG_REVISION = "1937049452c757dc346da01017ac50866fbeb169"
FORMAT = "draftbench-native-replay-v1"
REPLAY_REQUEST_FORMAT = "draftbench-review-replay-request-v1"
REPLAY_RESPONSE_FORMAT = "trust-growth-review-replay-v1"
MAX_OUTPUT_TOKENS = 16000
REASONING_EFFORT = "low"

_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def _string(description):
    return {"type": "string", "description": description}


SCHEMAS = {
    "SeoContentSchema": {
        "$schema": _SCHEMA,
        "title": "SeoContentSchema",
        "description": "An SEO-optimized long-form content piece. IMPORTANT: each "
        "field is independent — body is the complete article, meta_title and "
        "meta_description are separate standalone strings for search engine "
        "results.",
        "type": "object",
        "properties": {
            "title": _string(
                "The article headline. Standalone string, not part of the body."
            ),
            "body": _string(
                "The COMPLETE article in markdown. Must be 1000-3000 words. This is "
                "the full article text — do not continue the article into other "
                "fields."
            ),
            "meta_title": _string(
                "A separate, standalone meta title for search engine results pages. "
                "Complete, accurate and distinctive; preserve meaningful qualifiers. "
                "Character and pixel counts are observations, not editing limits. "
                "Do not mechanically cut or pad. This is NOT a continuation of the "
                "body."
            ),
            "meta_description": _string(
                "A separate, standalone meta description for search engine results. "
                "A complete, accurate summary specific to this page; preserve "
                "meaningful qualifiers. Character and pixel counts are observations, "
                "not editing limits. Do not mechanically cut or pad. This is NOT a "
                "continuation of the body."
            ),
            "tags": {
                "type": "array",
                "description": "3-8 topic tags as standalone keywords",
                "items": {"type": "string"},
            },
        },
        "required": ["title", "body", "meta_title", "meta_description", "tags"],
        "additionalProperties": False,
    },
    "ReviewLedgerSchema": {
        "$schema": _SCHEMA,
        "title": "ReviewLedgerSchema",
        "description": "Named review items, each settled by verbatim evidence from "
        "the draft",
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": _string("The item_id exactly as given"),
                        "state": {
                            "type": "string",
                            "enum": ["pass", "fail", "unknown"],
                            "description": "fail only with a verbatim quote from "
                            "the draft; unknown when the draft gives no basis",
                        },
                        "evidence": _string(
                            "For fail: the exact sentence from the draft (or the "
                            "title/meta text) that shows it. For unknown: what is "
                            "missing. Empty for pass."
                        ),
                        "detail": _string(
                            "One line naming the problem. Empty for pass."
                        ),
                        "owner": {
                            "type": "string",
                            "enum": ["writer", "brief", "none"],
                            "description": "promise_not_delivered only: brief when "
                            "no body could serve the target keyword; writer when the "
                            "body can be fixed. none otherwise.",
                        },
                    },
                    "required": ["item_id", "state", "evidence", "detail", "owner"],
                    "additionalProperties": False,
                },
            },
            "factual_issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": _string("The exact claim or number from the draft"),
                        "problem": _string("Why it is wrong or unverifiable"),
                    },
                    "required": ["claim", "problem"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["results", "factual_issues"],
        "additionalProperties": False,
    },
}
ROLE_SCHEMAS = {
    "writer": "SeoContentSchema",
    "reviewer": "ReviewLedgerSchema",
    "revision": "SeoContentSchema",
}


def wire_schema(name):
    """RubyLLM strips `$schema` and `title`; the title becomes the format name."""
    return {k: v for k, v in SCHEMAS[name].items() if k not in ("$schema", "title")}


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def native_messages(value):
    if (
        type(value) is not list
        or len(value) != 2
        or any(type(m) is not dict or set(m) != {"role", "content"} for m in value)
        or [m["role"] for m in value] != ["system", "user"]
        or any(type(m["content"]) is not str or not m["content"] for m in value)
    ):
        raise ValueError("native_roles_required")
    return [dict(m) for m in value]


def replay_prompt(messages, schema):
    if schema not in SCHEMAS:
        raise ValueError("unknown_replay_schema")
    return canonical_bytes(
        {"format": FORMAT, "messages": native_messages(messages), "schema": schema}
    ).decode()


def parse_prompt(prompt, max_bytes):
    if type(prompt) is not str or len(prompt.encode()) > max_bytes:
        raise ValueError("input_limit")
    value = strict_json_loads(prompt)
    if (
        type(value) is not dict
        or set(value) != {"format", "messages", "schema"}
        or value["format"] != FORMAT
        or value["schema"] not in SCHEMAS
    ):
        raise ValueError("invalid_replay_prompt")
    system, user = native_messages(value["messages"])
    return system["content"], user["content"], value["schema"]


def parse_draft(text):
    try:
        value = strict_json_loads(text)
    except (ValueError, TypeError):
        raise ValueError("draft_output_unparseable") from None
    if type(value) is not dict or any(
        type(value.get(k)) is not str for k in ("title", "body", "meta_description")
    ):
        raise ValueError("draft_output_unparseable")
    return value


# Mirrors the ERB in TG `review/ledger_items/user.txt.erb` (no trim mode): the
# `<% if %>`/`<% end %>` lines around the meta description each leave a newline.
_PROMISE = re.compile(
    r"\nTitle: [^\n]*\n\n(?:Meta description: [^\n]*\n\n)?Target keyword: "
)


def reviewer_messages(messages, exported_body, draft):
    """Substitute the lab draft into the exported TG reviewer payload.

    Refuses unless the exported TG draft body sits at exactly one anchor, so a
    template change fails closed instead of reviewing the wrong text.
    """
    system, user = native_messages(messages)
    text = user["content"]
    anchor = "\n## The draft\n" + exported_body + "\n\n## Items\n"
    promises = list(_PROMISE.finditer(text))
    if text.count(anchor) != 1 or len(promises) != 1:
        raise ValueError("reviewer_anchor_mismatch")
    start = text.index(anchor)
    promise = promises[0]
    if promise.end() > start:
        raise ValueError("reviewer_anchor_mismatch")
    meta = draft["meta_description"]
    replaced = (
        text[: promise.start()]
        + "\nTitle: "
        + draft["title"]
        + "\n\n"
        + (f"Meta description: {meta}\n\n" if meta.strip() else "")
        + "Target keyword: "
        + text[promise.end() : start]
        + "\n## The draft\n"
        + draft["body"]
        + "\n\n## Items\n"
        + text[start + len(anchor) :]
    )
    return [system, {"role": "user", "content": replaced}]


def review_item_ids(messages):
    text = native_messages(messages)[1]["content"]
    tail = text.rsplit("\n## Items\n", 1)
    ids = re.findall(r"^- item_id: (\S+)$", tail[-1], re.M) if len(tail) == 2 else []
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("reviewer_items_unavailable")
    return ids


def target_words(messages):
    found = re.findall(
        r"^- Target Word Count: ([1-9][0-9]{0,5})$",
        native_messages(messages)[1]["content"],
        re.M,
    )
    return int(found[0]) if len(found) == 1 else None


def body_words(body):
    for marker in ("\n## Sources", "\n## References", "\nSources:"):
        body = body.split(marker, 1)[0]
    return len(body.split())


def length_score(body, target):
    words = body_words(body)
    return {
        "body_words": words,
        "target_words": target,
        "ratio_to_target": None if target is None else round(words / target, 4),
    }


def replay_request(*, case_id, model, stage, draft_output, review_output):
    """The file the TG no-write review replay consumes; nothing is dispatched."""
    return {
        "format": REPLAY_REQUEST_FORMAT,
        "case_id": case_id,
        "model": model,
        "stage": stage,
        "tg_revision": TG_REVISION,
        "draft": strict_json_loads(draft_output),
        "draft_sha256": sha256_text(draft_output),
        "ledger": strict_json_loads(review_output),
        "review_sha256": sha256_text(review_output),
    }


def revision_messages(response, *, case_id, draft_output, review_output, system):
    """Validate a TG-rendered revision prompt bound to this exact lab chain."""
    if (
        type(response) is not dict
        or set(response)
        != {
            "format",
            "case_id",
            "tg_revision",
            "draft_sha256",
            "review_sha256",
            "review",
            "revision",
        }
        or response["format"] != REPLAY_RESPONSE_FORMAT
        or response["case_id"] != case_id
        or type(response["tg_revision"]) is not str
        or not re.fullmatch(r"[0-9a-f]{7,40}", response["tg_revision"])
        or type(response["review"]) is not dict
        or type(response["revision"]) is not dict
        or set(response["revision"]) != {"messages"}
    ):
        raise ValueError("invalid_review_replay")
    if response["draft_sha256"] != sha256_text(draft_output) or response[
        "review_sha256"
    ] != sha256_text(review_output):
        raise ValueError("review_replay_binding_mismatch")
    messages = native_messages(response["revision"]["messages"])
    if messages[0]["content"] != system:
        raise ValueError("review_replay_system_mismatch")
    return messages
