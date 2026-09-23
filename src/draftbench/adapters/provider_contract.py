"""SDK-free provider routing for policy, native request and saved custody."""

from . import anthropic_contract, openai_contract
from .openai_contract import digest as digest
from .pilot_policy import AnthropicPolicy, GPT6Policy, PilotPolicy


def parse_policy(value):
    if isinstance(value, PilotPolicy):
        value = value.model_dump(mode="json")
    cls = {
        "openai-gpt6-text-v1": GPT6Policy,
        "anthropic-messages-text-v1": AnthropicPolicy,
    }.get(value.get("contract"))
    if cls is None:
        raise ValueError("unsupported_provider_contract")
    return cls.model_validate(value)


def provider_name(policy):
    return "anthropic" if isinstance(policy, AnthropicPolicy) else "openai"


def transport_contract(policy):
    if isinstance(policy, AnthropicPolicy):
        return {
            "sdk_version": anthropic_contract.SDK_VERSION,
            "base_url": anthropic_contract.BASE_URL,
            "api": "messages",
            "anthropic_version": "2023-06-01",
        }
    return {
        "sdk_version": openai_contract.SDK_VERSION,
        "base_url": openai_contract.BASE_URL,
        "api": "chat.completions",
    }


def run_format(policy):
    return f"draftbench-{provider_name(policy)}-run-v1"


def native_request(policy, prompt):
    if isinstance(policy, AnthropicPolicy):
        return anthropic_contract.native_request(policy, prompt)
    return openai_contract.native_request(policy, prompt)


def verify_result(value, policy, prompt, provenance):
    if isinstance(policy, AnthropicPolicy):
        from .anthropic_result import verify_result as verify
    else:
        from .openai_result import verify_result as verify
    return verify(value, policy, prompt, provenance)


def validate_campaign_manifest(manifest, policy):
    if isinstance(policy, PilotPolicy):
        import re

        identity = manifest.get("campaign")
        if (
            type(identity) is not dict
            or set(identity) != {"campaign_id", "currency", "ceiling_usd"}
            or type(identity["campaign_id"]) is not str
            or not re.fullmatch(r"[0-9a-f]{32}", identity["campaign_id"])
            or identity["currency"] != "USD"
            or identity["ceiling_usd"] != "50.00"
        ):
            raise ValueError("campaign_required")
