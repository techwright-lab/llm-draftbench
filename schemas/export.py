"""Regenerate structural schemas from the authoritative offline models."""

import json
from pathlib import Path

from draftbench.adapters.anthropic_result import AnthropicResult
from draftbench.adapters.inspect import InspectPolicy, InspectResult
from draftbench.adapters.openai_contract import OpenAIPolicy
from draftbench.adapters.openai_result import OpenAIResult
from draftbench.adapters.pilot_policy import AnthropicPolicy, GPT6Policy
from draftbench.annotation_models import (
    AdjudicationBatch,
    AnnotationManifest,
    AnnotationRubric,
    BlindPacket,
    RaterRoster,
    ResponseBatch,
)
from draftbench.models import Case, Suite
from draftbench.scoring.models import ScoringBundle
from draftbench.workflow import RunManifest


def main():
    directory = Path(__file__).resolve().parent
    for name, model in (
        ("openai-policy", OpenAIPolicy),
        ("openai-result", OpenAIResult),
        ("gpt6-policy", GPT6Policy),
        ("anthropic-policy", AnthropicPolicy),
        ("anthropic-result", AnthropicResult),
        ("inspect-policy", InspectPolicy),
        ("inspect-result", InspectResult),
        ("run-manifest", RunManifest),
        ("case", Case),
        ("suite", Suite),
        ("scoring", ScoringBundle),
        ("annotation-rubric", AnnotationRubric),
        ("raters", RaterRoster),
        ("blind-packet", BlindPacket),
        ("annotation-responses", ResponseBatch),
        ("adjudication-responses", AdjudicationBatch),
        ("annotation-session", AnnotationManifest),
    ):
        (directory / f"{name}.schema.json").write_text(
            json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
