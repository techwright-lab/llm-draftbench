"""In-process fixture transport. This module contains no HTTP/provider client."""

from inspect_ai.model import (
    ChatCompletionChoice,
    ChatMessageAssistant,
    ModelAPI,
    ModelOutput,
    modelapi,
)


@modelapi(name="draftbench-fixture")
class FixtureAPI(ModelAPI):
    def __init__(
        self, model_name="draftbench-fixture", *, completion="Synthetic fixture"
    ):
        if model_name != "draftbench-fixture":
            raise ValueError("live_execution_disabled")
        super().__init__(model_name=model_name)
        self.completion = completion

    async def generate(self, input, tools, tool_choice, config):
        if (
            tools
            or config.max_retries != 0
            or config.num_choices != 1
            or config.best_of != 1
            or config.cache
            or config.batch
        ):
            raise ValueError("unsafe_sdk_configuration")
        # Byte-counting is a deliberately conservative fixture bound, not a
        # tokenizer or measured provider usage. No tokenizer download is needed.
        raw = self.completion.encode("utf-8")
        limited = len(raw) > config.max_tokens
        content = raw[: config.max_tokens].decode("utf-8", errors="ignore")
        return ModelOutput(
            model=self.model_name,
            choices=[
                ChatCompletionChoice(
                    message=ChatMessageAssistant(content=content),
                    stop_reason="max_tokens" if limited else "stop",
                )
            ],
        )
