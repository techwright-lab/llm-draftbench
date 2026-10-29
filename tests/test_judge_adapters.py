import json

import pytest
from test_judge_panel import BRIEF, CANDIDATES, GOOGLE, JUDGES

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
    assert calls[0].headers["x-api-key"] == "fixture-not-a-credential"
    assert "authorization" not in calls[0].headers
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


def jev_score_native(level=2):
    probs = {"0": 0.05, "1": 0.05, "2": 0.05, "3": 0.05}
    probs[str(level)] = 0.85
    return {
        "model": "jev-1.14.0",
        "answers": {
            cid: {
                "type": "score",
                "score": float(level),
                "confidence": 0.8,
                "legend": {},
                "probabilities": probs,
            }
            for cid in CRITERION_IDS
        },
        "usage": {"input_tokens": 900, "output_tokens": 0},
    }


def test_jev_pointwise_wire_and_parse():
    import httpx

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=jev_score_native(3))

    p = panel()
    cid = sorted(p["candidates"])[0]
    result = jev.invoke_score(
        p["judges"][0], p, cid, transport=httpx.MockTransport(handler)
    )
    body = json.loads(calls[0].content)
    assert set(body["state"]) == {"brief", "draft"}
    assert body["state"]["draft"] == p["candidates"][cid]["body"]
    assert body["questions"]["clarity"]["type"] == "score"
    assert len(body["questions"]["clarity"]["criteria"]) == 4
    assert body["questions"]["clarity"]["instructions"].startswith("Does the draft")
    assert result["status"] == "success"
    assert result["verdicts"]["clarity"] == {
        "score": 3.0,
        "levels": 4,
        "probabilities": {"0": 0.05, "1": 0.05, "2": 0.05, "3": 0.85},
        "confidence": 0.8,
    }


def test_jev_pointwise_rejects_out_of_range_score():
    import httpx

    native = jev_score_native(2)
    native["answers"]["clarity"]["score"] = 7
    p = panel()
    result = jev.invoke_score(
        p["judges"][0],
        p,
        sorted(p["candidates"])[0],
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=native)),
    )
    assert (
        result["status"] == "uncertain"
        and result["error_code"] == "invalid_native_response"
    )


def google_native(text, finish="STOP"):
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "private reasoning", "thought": True},
                        {"text": text},
                    ],
                },
                "finishReason": finish,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 80,
            "candidatesTokenCount": 25,
            "thoughtsTokenCount": 15,
            "totalTokenCount": 120,
        },
        "modelVersion": "gemini-4-argon",
        "responseId": "resp_g",
    }


def google_panel():
    return build_panel(brief=BRIEF, candidates=CANDIDATES, judges=[GOOGLE])[0]


def test_google_judge_wire_and_parse():
    import httpx

    pytest.importorskip("google.genai")
    p = google_panel()
    presentation = p["presentations"][0]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json=google_native(verdict_json(p, presentation, "a", "a"))
        )

    result = generative.invoke(
        p["judges"][0], p, presentation, transport=httpx.MockTransport(handler)
    )
    assert len(calls) == 1
    assert (
        str(calls[0].url)
        == "https://generativelanguage.googleapis.com/v1beta/models/gemini-4-argon:generateContent"
    )
    assert calls[0].headers["x-goog-api-key"] == "fixture-not-a-credential"
    body = json.loads(calls[0].content)
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"]["required"] == ["verdicts"]
    assert config["maxOutputTokens"] == 8000
    assert config["thinkingConfig"] == {"thinking_level": "LOW"}
    assert body["systemInstruction"]["parts"][0]["text"].startswith(
        "You compare two drafts"
    )
    assert "## Draft A" in body["contents"][0]["parts"][0]["text"]
    assert result["status"] == "success", result.get("error_code")
    assert (
        result["served_model"] == "gemini-4-argon" and result["request_id"] == "resp_g"
    )
    assert result["verdicts"]["clarity"]["choice"] == "a"
    assert result["usage"] == {
        "input_tokens": 80,
        "output_tokens": 40,
        "thinking_tokens": 15,
        "cached_input_tokens": None,
    }


def test_google_judge_truncation_and_auth():
    import httpx

    pytest.importorskip("google.genai")
    p = google_panel()
    presentation = p["presentations"][0]
    truncated = generative.invoke(
        p["judges"][0],
        p,
        presentation,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=google_native("{", finish="MAX_TOKENS"))
        ),
    )
    assert (
        truncated["status"] == "uncertain"
        and truncated["error_code"] == "invalid_native_response"
    )
    rejected = generative.invoke(
        p["judges"][0],
        p,
        presentation,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "x",
                        "status": "PERMISSION_DENIED",
                    }
                },
            )
        ),
    )
    assert rejected["error_code"] == "auth_rejected"
    server = generative.invoke(
        p["judges"][0],
        p,
        presentation,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                500, json={"error": {"code": 500, "message": "x", "status": "INTERNAL"}}
            )
        ),
    )
    assert server["error_code"] == "provider_http_error"
