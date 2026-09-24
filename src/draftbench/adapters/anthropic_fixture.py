"""Synthetic Messages fixture; never a live fallback."""

import json

from .openai_fixture import fixture_output


def fixture_transport(model):
    import httpx

    def respond(request):
        wire = json.loads(request.content)
        schema = wire["output_config"]["format"]["schema"]
        return httpx.Response(
            200,
            headers={"request-id": "fixture-request"},
            json={
                "id": "fixture-message",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [
                    {"type": "redacted_thinking", "data": "opaque-synthetic-fixture"},
                    {
                        "type": "text",
                        "text": fixture_output("results" in schema["properties"]),
                    },
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": None,
            },
        )

    return httpx.MockTransport(respond)
