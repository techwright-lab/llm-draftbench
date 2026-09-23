"""Synthetic Messages fixture; never a live fallback."""


def fixture_transport(model):
    import httpx

    def respond(request):
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
                    {"type": "text", "text": "Synthetic Anthropic wire fixture."},
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": None,
            },
        )

    return httpx.MockTransport(respond)
