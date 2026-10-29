import json

import pytest
from test_judge_panel import BRIEF, CANDIDATES, JUDGES

from draftbench.judge import generative, jev
from draftbench.judge.criteria import CRITERION_IDS
from draftbench.judge.panel import build_panel

pytest.importorskip("httpx")


def panel():
    return build_panel(brief=BRIEF, candidates=CANDIDATES, judges=JUDGES)[0]


def jev_answer(choice="a", confidence=0.8):
    probs = {"a": 0.1, "b": 0.1, "tie": 0.1}
    probs[choice] = 0.8
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": probs,
    }


def jev_native(choice="a"):
    return {
        "model": "jev-1.14.0",
        "answers": {cid: jev_answer(choice) for cid in CRITERION_IDS},
        "usage": {"input_tokens": 1200, "output_tokens": 0},
    }


def test_jev_wire_and_parse():
    import httpx

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=jev_native("b"))

    p = panel()
    presentation = p["presentations"][0]
    result = jev.invoke(
        p["judges"][0], p, presentation, transport=httpx.MockTransport(handler)
    )
    assert calls[0].url == "https://api.typesafe.ai/v1/systemone"
    assert calls[0].headers["authorization"] == "Bearer fixture-not-a-credential"
    body = json.loads(calls[0].content)
    assert body["model"] == "jev-1.14.0"
    assert body["state"]["draft_a"] == p["candidates"][presentation["a"]]["body"]
    assert set(body["questions"]) == set(CRITERION_IDS)
    assert body["questions"]["clarity"]["type"] == "choice"
    assert set(body["questions"]["clarity"]["criteria"]) == {"a", "b", "tie"}
    assert result["status"] == "success"
    assert result["verdicts"]["insight"]["choice"] == "b"
    assert result["usage"] == {"input_tokens": 1200, "output_tokens": 0}
    assert result["estimated_usd"] == "0.0000504"
    assert result["provider_latency_seconds"] is None


@pytest.mark.parametrize(
    "native, status, code",
    [
        (
            {"model": "jev-1.14.0", "answers": {}},
            "uncertain",
            "invalid_native_response",
        ),
        (
            {
                "model": "jev-1.14.0",
                "answers": {
                    cid: {**jev_answer(), "probabilities": {"a": 2, "b": 0, "tie": 0}}
                    for cid in CRITERION_IDS
                },
            },
            "uncertain",
            "invalid_native_response",
        ),
    ],
)
def test_jev_rejects_malformed_answers(native, status, code):
    import httpx

    p = panel()
    result = jev.invoke(
        p["judges"][0],
        p,
        p["presentations"][0],
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=native)),
    )
    assert result["status"] == status and result["error_code"] == code
    assert result["verdicts"] is None


def test_jev_auth_rejection_is_flagged():
    import httpx

    p = panel()
    result = jev.invoke(
        p["judges"][0],
        p,
        p["presentations"][0],
        transport=httpx.MockTransport(
            lambda r: httpx.Response(401, json={"detail": "no"})
        ),
    )
    assert result["error_code"] == "auth_rejected" and result["status"] == "uncertain"


def test_jev_live_requires_key():
    p = panel()
    with pytest.raises(ValueError, match="live_authorization_required"):
        jev.invoke(p["judges"][0], p, p["presentations"][0])


def verdict_json(p, presentation, evidence_from="a", choice="a"):
    quote = p["candidates"][presentation[evidence_from]]["body"].split(". ")[0] + "."
    return json.dumps(
        {
            "verdicts": [
                {"criterion": cid, "choice": choice, "evidence": quote, "reason": "r"}
                for cid in CRITERION_IDS
            ]
        }
    )


def test_openai_judge_wire_and_evidence_check():
    import httpx
    from replay_fixtures import openai_native, openai_usage

    pytest.importorskip("openai")
    p = panel()
    presentation = p["presentations"][0]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json=openai_native(
                "gpt-6.1-sol",
                text=verdict_json(p, presentation, "b", "b"),
                usage=openai_usage(i=50, o=20),
            ),
            headers={"x-request-id": "req_j"},
        )

    result = generative.invoke(
        p["judges"][1], p, presentation, transport=httpx.MockTransport(handler)
    )
    body = json.loads(calls[0].content)
    assert calls[0].url == "https://api.openai.com/v1/responses"
    assert (
        body["text"]["format"]["name"] == "PairwiseVerdictSchema"
        and body["text"]["format"]["strict"] is True
    )
    assert body["store"] is False and body["reasoning"] == {"effort": "low"}
    assert (
        "## Draft A" in body["input"][0]["content"]
        and BRIEF in body["input"][0]["content"]
    )
    assert result["status"] == "success" and result["request_id"] == "req_j"
    assert result["verdicts"]["clarity"]["choice"] == "b"
    assert result["verdicts"]["clarity"]["evidence_in"] == ["b"]
    assert result["usage"] == {"input_tokens": 50, "output_tokens": 20}


def test_generative_verdict_without_verbatim_evidence_is_unknown():
    import httpx
    from replay_fixtures import openai_native, openai_usage

    pytest.importorskip("openai")
    p = panel()
    presentation = p["presentations"][0]
    text = json.dumps(
        {
            "verdicts": [
                {
                    "criterion": cid,
                    "choice": "a",
                    "evidence": "Not in any draft.",
                    "reason": "r",
                }
                for cid in CRITERION_IDS
            ]
        }
    )
    result = generative.invoke(
        p["judges"][1],
        p,
        presentation,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json=openai_native("gpt-6.1-sol", text=text, usage=openai_usage())
            )
        ),
    )
    assert result["status"] == "success"
    assert {v["choice"] for v in result["verdicts"].values()} == {"unknown"}


def test_anthropic_judge_wire():
    import httpx

    pytest.importorskip("anthropic")
    p = panel()
    presentation = p["presentations"][1]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_j",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [
                    {"type": "text", "text": verdict_json(p, presentation, "a", "tie")}
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 70,
                    "output_tokens": 30,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
            headers={"request-id": "req_a"},
        )

    result = generative.invoke(
        p["judges"][2], p, presentation, transport=httpx.MockTransport(handler)
    )
    body = json.loads(calls[0].content)
    assert calls[0].url == "https://api.anthropic.com/v1/messages"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["max_tokens"] == 4000 and body["thinking"] == {"type": "adaptive"}
    assert (
        result["status"] == "success"
        and result["verdicts"]["insight"]["choice"] == "tie"
    )
    assert result["usage"]["input_tokens"] == 70


def test_generative_auth_rejection_is_flagged():
    import httpx

    pytest.importorskip("openai")
    p = panel()
    result = generative.invoke(
        p["judges"][1],
        p,
        p["presentations"][0],
        transport=httpx.MockTransport(
            lambda r: httpx.Response(401, json={"error": {"message": "x"}})
        ),
    )
    assert result["error_code"] == "auth_rejected"
