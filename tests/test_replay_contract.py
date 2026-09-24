"""Pure native-replay contract: roles, pinned schemas, substitution, seam."""

import json

import pytest
from replay_fixtures import (
    ITEMS,
    PROMPT,
    SIDECAR_CANARY,
    SUITE,
    SYSTEM,
    USER,
    case,
    draft_json,
    review_json,
    review_replay,
)

from draftbench.adapters import replay_contract as replay
from draftbench.adapters.pilot_policy import AnthropicPolicy, GPT6Policy

COMMON = dict(
    account_route="fixture-only",
    currency="USD",
    max_cost="50",
    max_output_tokens=16000,
    max_requests=4,
    max_total_tokens=4100000,
    pricing_provenance="public-docs-2026-09-23-v1",
)


def messages(role):
    return case()["generator"][role]["input"]["messages"]


def exported_body():
    return case()["history"]["drafts"][0]["units"][0]["content"]


def test_pinned_schemas_are_strict_local_tg_shapes():
    from jsonschema import Draft202012Validator

    for name, schema in replay.SCHEMAS.items():
        Draft202012Validator.check_schema(schema)
        assert "$ref" not in json.dumps(schema)
        assert schema["title"] == name
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["additionalProperties"] is False
        wire = replay.wire_schema(name)
        assert "$schema" not in wire and "title" not in wire
    writer = Draft202012Validator(replay.SCHEMAS["SeoContentSchema"])
    reviewer = Draft202012Validator(replay.SCHEMAS["ReviewLedgerSchema"])
    assert writer.is_valid(json.loads(draft_json()))
    assert reviewer.is_valid(json.loads(review_json()))
    assert not writer.is_valid(json.loads(draft_json()) | {"extra": 1})
    assert replay.ROLE_SCHEMAS == {
        "writer": "SeoContentSchema",
        "reviewer": "ReviewLedgerSchema",
        "revision": "SeoContentSchema",
    }


@pytest.mark.parametrize(
    "value",
    [
        [{"role": "user", "content": USER}],
        [{"role": "user", "content": USER}, {"role": "system", "content": SYSTEM}],
        [{"role": "system", "content": ""}, {"role": "user", "content": USER}],
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER, "name": "x"},
        ],
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER},
            {"role": "assistant", "content": "prefill"},
        ],
    ],
)
def test_only_exported_system_then_user_roles(value):
    with pytest.raises(ValueError, match="native_roles_required"):
        replay.replay_prompt(value, "SeoContentSchema")


def test_prompt_parse_rejects_legacy_envelope_and_cap():
    assert replay.parse_prompt(PROMPT, 100000) == (SYSTEM, USER, "SeoContentSchema")
    legacy = json.dumps({"input": {}, "role": "writer", "parent_outputs": {}})
    with pytest.raises(ValueError, match="invalid_replay_prompt"):
        replay.parse_prompt(legacy, 100000)
    with pytest.raises(ValueError, match="input_limit"):
        replay.parse_prompt(PROMPT, len(PROMPT.encode()) - 1)


def test_native_requests_per_provider():
    from draftbench.adapters.anthropic_contract import native_request as anthropic
    from draftbench.adapters.openai_contract import native_request as openai

    gpt = GPT6Policy.model_validate(
        COMMON
        | dict(
            model="gpt-6-sol",
            organization="org_x",
            project="proj_x",
            reasoning_effort="low",
        )
    )
    assert openai(gpt, PROMPT) == {
        "model": "gpt-6-sol",
        "instructions": SYSTEM,
        "input": [{"role": "user", "content": USER}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "SeoContentSchema",
                "schema": replay.wire_schema("SeoContentSchema"),
                "strict": True,
            }
        },
        "max_output_tokens": 16000,
        "reasoning": {"effort": "low"},
        "stream": False,
        "store": False,
        "service_tier": "default",
    }
    claude = AnthropicPolicy.model_validate(
        COMMON | dict(model="claude-sonnet-5", effort="low")
    )
    assert anthropic(claude, PROMPT) == {
        "model": "claude-sonnet-5",
        "system": [{"type": "text", "text": SYSTEM}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": USER}]}],
        "max_tokens": 16000,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "low",
            "format": {
                "type": "json_schema",
                "schema": replay.wire_schema("SeoContentSchema"),
            },
        },
        "stream": False,
        "service_tier": "standard_only",
        "inference_geo": "global",
    }


def test_reviewer_substitutes_lab_draft_at_tg_anchors():
    draft = json.loads(draft_json("Lab body.\n\n## Items\nnot an anchor"))
    system, user = replay.reviewer_messages(
        messages("reviewer"), exported_body(), draft
    )
    assert system == messages("reviewer")[0]
    text = user["content"]
    assert exported_body() not in text
    assert "Synthetic exported title" not in text
    assert (
        "\nTitle: Synthetic title\n\nMeta description: Synthetic meta description."
        "\n\nTarget keyword: " in text
    )
    assert "\n## The draft\nLab body.\n\n## Items\nnot an anchor\n\n## Items\n" in text
    assert replay.review_item_ids([system, user]) == list(ITEMS)


def test_blank_lab_meta_description_drops_the_line_like_tg():
    draft = json.loads(draft_json()) | {"meta_description": "  "}
    text = replay.reviewer_messages(messages("reviewer"), exported_body(), draft)[1][
        "content"
    ]
    assert "Meta description:" not in text
    assert "\nTitle: Synthetic title\n\nTarget keyword: " in text


def test_missing_exported_meta_line_is_inserted():
    exported = messages("reviewer")
    exported[1]["content"] = exported[1]["content"].replace(
        "Meta description: Synthetic exported meta description.\n\n", ""
    )
    text = replay.reviewer_messages(
        exported, exported_body(), json.loads(draft_json())
    )[1]["content"]
    assert "Meta description: Synthetic meta description.\n\nTarget keyword" in text


@pytest.mark.parametrize("change", ["body", "promise", "duplicate", "order"])
def test_reviewer_anchor_mismatch_refused(change):
    exported, body = messages("reviewer"), exported_body()
    text = exported[1]["content"]
    if change == "body":
        body = body + " changed"
    elif change == "promise":
        exported[1]["content"] = text.replace("Title: ", "Headline: ")
    elif change == "duplicate":
        exported[1]["content"] = text + "\n## The draft\n" + body + "\n\n## Items\n"
    else:
        head, tail = text.split("## Sibling drafts", 1)
        exported[1]["content"] = tail + head
    with pytest.raises(ValueError, match="reviewer_anchor_mismatch"):
        replay.reviewer_messages(exported, body, json.loads(draft_json()))


def test_target_words_and_length_score():
    assert replay.target_words(messages("writer")) == 1200
    assert replay.target_words([messages("writer")[0], messages("reviewer")[1]]) is (
        None
    )
    body = "one two three\n## Sources\n- ignored source"
    assert replay.length_score(body, 1200) == {
        "body_words": 3,
        "target_words": 1200,
        "ratio_to_target": 0.0025,
    }
    assert replay.length_score(body, None)["ratio_to_target"] is None


def test_review_replay_request_and_response_binding():
    draft, review = draft_json(), review_json()
    request = replay.replay_request(
        case_id="synthetic-replay-1",
        model="gpt-6-sol",
        stage="draft",
        draft_output=draft,
        review_output=review,
    )
    assert request["format"] == "draftbench-review-replay-request-v1"
    assert request["draft"] == json.loads(draft)
    assert request["ledger"] == json.loads(review)
    assert request["tg_revision"] == replay.TG_REVISION
    response = review_replay(draft, review)
    assert response["draft_sha256"] == request["draft_sha256"]
    system = messages("revision")[0]["content"]
    bound = dict(case_id="synthetic-replay-1", system=system)
    result = replay.revision_messages(
        response, draft_output=draft, review_output=review, **bound
    )
    assert result == response["revision"]["messages"]
    with pytest.raises(ValueError, match="review_replay_binding_mismatch"):
        replay.revision_messages(
            response, draft_output=draft + " ", review_output=review, **bound
        )
    with pytest.raises(ValueError, match="review_replay_system_mismatch"):
        replay.revision_messages(
            response,
            draft_output=draft,
            review_output=review,
            case_id="synthetic-replay-1",
            system="Other system.",
        )
    for broken in (
        response | {"extra": 1},
        response | {"case_id": "other"},
        response | {"format": "other"},
        response | {"review": None},
        response | {"revision": {"messages": [], "extra": 1}},
    ):
        with pytest.raises(ValueError, match="invalid_review_replay"):
            replay.revision_messages(
                broken, draft_output=draft, review_output=review, **bound
            )


def test_sidecar_and_evidence_never_become_inputs():
    from draftbench.loader import load_suite
    from draftbench.models import Case
    from draftbench.provider_workflow import _input

    loaded = load_suite(SUITE).cases[0]
    assert SIDECAR_CANARY in (SUITE.parent / "source.json").read_text()
    for role in ("writer", "reviewer", "revision"):
        value = _input(loaded, role)
        assert set(value) == {"prompt_version", "messages", "drafts"}
        assert SIDECAR_CANARY not in json.dumps(value)
    data = loaded.model_dump(mode="json")
    artifact = data["source"]["artifact"] | {"artifact_id": "evidence-1"}
    data["evidence"] = {"state": "available", "artifacts": [artifact]}
    data["generator"]["writer"]["input"]["evidence_ids"] = ["evidence-1"]
    from draftbench.identity import identity

    data["identity"] = identity(data)
    with pytest.raises(ValueError, match="evidence_not_supported"):
        _input(Case.model_validate(data), "writer")
