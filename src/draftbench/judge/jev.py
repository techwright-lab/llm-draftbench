"""TypeSafe System One (Jev) judge: typed choice answers over plain HTTPS."""

import math
import time

from .criteria import CHOICE_DESCRIPTIONS, CHOICES, SCORE_LEVELS

BASE_URL = "https://api.typesafe.ai"
ENDPOINT = BASE_URL + "/v1/systemone"
MODEL = "jev-1.14.0"
INPUT_USD_PER_MILLION = "0.042"
PRICING_PROVENANCE = "docs.typesafe.ai/models accessed 2026-10-29"
TIMEOUT_SECONDS = 120.0


def native_request(judge, panel, presentation):
    """One request per presentation; every criterion is a separate choice."""
    a, b = (
        panel["candidates"][presentation["a"]],
        panel["candidates"][presentation["b"]],
    )
    questions = {}
    for item in panel["criteria"]:
        questions[item["criterion_id"]] = {
            "type": "choice",
            "instructions": item["question"],
            "criteria": dict(CHOICE_DESCRIPTIONS),
        }
    return {
        "model": judge["model"],
        "state": {
            "brief": panel["brief"],
            "draft_a": a["body"],
            "draft_b": b["body"],
        },
        "questions": questions,
    }


def native_score_request(judge, panel, candidate_id):
    """One request per draft; every criterion is an ordered score, no positions."""
    questions = {}
    for item in panel["criteria"]:
        questions[item["criterion_id"]] = {
            "type": "score",
            "instructions": item["question"].replace("Which draft", "Does the draft"),
            "criteria": list(SCORE_LEVELS[item["criterion_id"]]),
        }
    body = panel["candidates"][candidate_id]["body"]
    if panel.get("subject") == "brief":
        state = {"writer_constraints": panel["brief"], "writer_input": body}
    else:
        state = {"brief": panel["brief"], "draft": body}
    return {"model": judge["model"], "state": state, "questions": questions}


def invoke(judge, panel, presentation, *, transport=None, api_key=None):
    """Return a content-free result; the native body stays in the private ledger."""
    request = native_request(judge, panel, presentation)
    return _post(judge, request, _verdicts, transport=transport, api_key=api_key)


def invoke_score(judge, panel, candidate_id, *, transport=None, api_key=None):
    request = native_score_request(judge, panel, candidate_id)
    return _post(judge, request, _scores, transport=transport, api_key=api_key)


def _post(judge, request, parse, *, transport, api_key):
    import httpx

    if transport is not None and type(transport) is not httpx.MockTransport:
        raise ValueError("fixture_transport_required")
    if transport is None and (type(api_key) is not str or not api_key):
        raise ValueError("live_authorization_required")
    if judge["model"] != MODEL:
        raise ValueError("unsupported_judge_model")
    result = {
        "adapter": "typesafe",
        "provenance": "fixture" if transport is not None else "provider",
        "requested_model": judge["model"],
        "served_model": None,
        "native_output": None,
        "verdicts": None,
        "usage": None,
        "estimated_usd": None,
        "provider_latency_seconds": None,
        "status": "uncertain",
    }
    start = time.monotonic()
    try:
        with httpx.Client(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=TIMEOUT_SECONDS,
        ) as http:
            response = http.post(
                ENDPOINT,
                json=request,
                headers={
                    "Authorization": "Bearer "
                    + (
                        "fixture-not-a-credential" if transport is not None else api_key
                    ),
                    "Accept": "application/json",
                },
            )
        result["http_status"] = response.status_code
        if response.status_code in (401, 403):
            result["error_code"] = "auth_rejected"
            return result
        if response.status_code != 200:
            result["error_code"] = "provider_http_error"
            return result
        native = response.json()
        result["native_output"] = native
        result["served_model"] = native.get("model")
        result["verdicts"] = parse(native, request)
        result["usage"] = _usage(native)
        result["estimated_usd"] = _estimate(result["usage"])
        result["status"] = "success"
    except Exception as exc:
        import httpx as _httpx

        result["error_code"] = (
            "provider_timeout"
            if isinstance(exc, _httpx.TimeoutException)
            else "provider_connection_error"
            if isinstance(exc, _httpx.TransportError)
            else "invalid_native_response"
        )
        result.update(verdicts=None, usage=None, estimated_usd=None, status="uncertain")
    finally:
        result["provider_latency_seconds"] = (
            None if transport is not None else round(time.monotonic() - start, 3)
        )
    return result


def _verdicts(native, request):
    answers = native.get("answers")
    if type(native) is not dict or type(answers) is not dict:
        raise ValueError("invalid_native_response")
    if set(answers) != set(request["questions"]):
        raise ValueError("invalid_native_response")
    verdicts = {}
    for criterion_id, answer in answers.items():
        probabilities = answer.get("probabilities")
        if (
            type(answer) is not dict
            or answer.get("type") != "choice"
            or answer.get("choice") not in CHOICES
            or type(probabilities) is not dict
            or set(probabilities) != set(CHOICES)
        ):
            raise ValueError("invalid_native_response")
        values = {}
        for key, value in probabilities.items():
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError("invalid_native_response")
            values[key] = float(value)
        if abs(sum(values.values()) - 1.0) > 0.05:
            raise ValueError("invalid_native_response")
        confidence = answer.get("confidence")
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            raise ValueError("invalid_native_response")
        verdicts[criterion_id] = {
            "choice": answer["choice"],
            "probabilities": values,
            "confidence": float(confidence),
            "evidence": None,
        }
    return verdicts


def _scores(native, request):
    answers = native.get("answers")
    if type(native) is not dict or type(answers) is not dict:
        raise ValueError("invalid_native_response")
    if set(answers) != set(request["questions"]):
        raise ValueError("invalid_native_response")
    scores = {}
    for criterion_id, answer in answers.items():
        levels = len(request["questions"][criterion_id]["criteria"])
        probabilities = answer.get("probabilities")
        score = answer.get("score")
        if (
            type(answer) is not dict
            or answer.get("type") != "score"
            or type(score) not in (int, float)
            or not math.isfinite(score)
            or not 0 <= score <= levels - 1
            or type(probabilities) is not dict
            or set(probabilities) != {str(i) for i in range(levels)}
        ):
            raise ValueError("invalid_native_response")
        values = {}
        for key, value in probabilities.items():
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError("invalid_native_response")
            values[key] = float(value)
        if abs(sum(values.values()) - 1.0) > 0.05:
            raise ValueError("invalid_native_response")
        confidence = answer.get("confidence")
        if type(confidence) not in (int, float) or not 0 <= confidence <= 1:
            raise ValueError("invalid_native_response")
        scores[criterion_id] = {
            "score": float(score),
            "levels": levels,
            "probabilities": values,
            "confidence": float(confidence),
        }
    return scores


def _usage(native):
    usage = native.get("usage")
    if type(usage) is not dict:
        return None
    out = {}
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if type(value) is not int or value < 0:
            return None
        out[key] = value
    return out


def _estimate(usage):
    if usage is None:
        return None
    from decimal import Decimal

    cost = (
        Decimal(usage["input_tokens"])
        * Decimal(INPUT_USD_PER_MILLION)
        / Decimal(1_000_000)
    )
    return str(cost)
