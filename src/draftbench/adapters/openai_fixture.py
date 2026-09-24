"""SDK wire fixture, never a provider fallback."""

import json


def fixture_output(reviewer):
    return json.dumps(
        {"results": [], "factual_issues": []}
        if reviewer
        else {
            "title": "Synthetic fixture title",
            "body": "Synthetic fixture body.",
            "meta_title": "Synthetic fixture meta title",
            "meta_description": "Synthetic fixture meta description.",
            "tags": ["synthetic"],
        },
        sort_keys=True,
    )


def fixture_transport(model):
    import httpx

    def respond(request):
        wire = json.loads(request.content)
        name = wire["text"]["format"]["name"]
        return httpx.Response(
            200,
            headers={"x-request-id": "fixture-request"},
            json={
                "id": "fixture-response",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "model": model,
                "output": [
                    {
                        "id": "fixture-message",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": fixture_output(name == "ReviewLedgerSchema"),
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": None,
            },
        )

    return httpx.MockTransport(respond)
