"""SDK wire fixture, never a provider fallback."""


def fixture_transport(model):
    import httpx

    def respond(request):
        return httpx.Response(
            200,
            headers={"x-request-id": "fixture-request"},
            json={
                "id": "fixture-completion",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "Synthetic OpenAI wire fixture.",
                        },
                    }
                ],
                "usage": None,
            },
        )

    return httpx.MockTransport(respond)
