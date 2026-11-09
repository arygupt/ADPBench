"""The reviewed model providers a plan may call.

A plan model names its provider by ID (`"provider": "anthropic"`); without one
it is OpenCode Go, so every earlier plan and receipt reads as before. Hosts,
paths and credential variables live only in this file: a plan can choose
among them but can never point a key at a new URL. Adding a provider is a
code review.

The provider ID is also the prefix of a model's run label
(`anthropic/claude-fable-5-1 [agent-assisted-v2]`) and of its result
directory (`anthropic-claude-fable-5-1/`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    host: str
    path: str  # joined with the model's API name, e.g. "/v1/" + "messages"
    key_env: str  # the only environment variable its credential is read from
    apis: frozenset[str]
    # "subscription": a flat plan whose paid balance must stay off.
    # "metered": every request is billed per token.
    billing: str
    auth: str = "bearer"  # or "x-api-key"
    session_header: str = ""
    # HTTP statuses, beyond the protocol's own, that mean "resend unchanged".
    retry_statuses: frozenset[int] = frozenset()


GO = Provider(
    id="opencode-go",
    name="OpenCode Go",
    host="opencode.ai",
    path="/zen/go/v1/",
    key_env="OPENCODE_GO_API_KEY",
    apis=frozenset({"chat/completions", "messages", "responses"}),
    billing="subscription",
    session_header="x-opencode-session",
)

PROVIDERS = {
    provider.id: provider
    for provider in (
        GO,
        Provider(
            id="anthropic",
            name="Anthropic",
            host="api.anthropic.com",
            path="/v1/",
            key_env="ANTHROPIC_API_KEY",
            apis=frozenset({"messages"}),
            billing="metered",
            auth="x-api-key",
            retry_statuses=frozenset({529}),  # overloaded_error
        ),
        Provider(
            id="openai",
            name="OpenAI",
            host="api.openai.com",
            path="/v1/",
            key_env="OPENAI_API_KEY",
            apis=frozenset({"responses", "chat/completions"}),
            billing="metered",
        ),
        Provider(
            id="xai",
            name="xAI",
            host="api.x.ai",
            path="/v1/",
            key_env="XAI_API_KEY",
            apis=frozenset({"responses", "chat/completions"}),
            billing="metered",
        ),
        Provider(
            id="moonshot",
            name="Moonshot AI",
            host="api.moonshot.ai",
            path="/v1/",
            key_env="MOONSHOT_API_KEY",
            apis=frozenset({"chat/completions"}),
            billing="metered",
        ),
    )
}
KEY_ENVS = tuple(provider.key_env for provider in PROVIDERS.values())


def provider_of(model: dict) -> Provider:
    """The provider a plan model is called through (OpenCode Go unless it names one)."""
    provider_id = model.get("provider", GO.id)
    if not isinstance(provider_id, str) or provider_id not in PROVIDERS:
        raise ValueError("unknown model provider")
    return PROVIDERS[provider_id]


def provider_key(model: dict) -> str:
    """The credential for a model's provider, or "" if it is not configured."""
    return os.environ.get(provider_of(model).key_env, "")


def auth_headers(provider: Provider, api: str, key: str, session: str) -> dict:
    """The headers that carry the credential and identify the session."""
    headers = {}
    if provider.auth == "x-api-key":
        headers["x-api-key"] = key
    else:
        headers["Authorization"] = "Bearer " + key
    if api == "messages":
        # Anthropic's wire format; Go's messages endpoint expects it as well.
        headers["x-api-key"] = key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    if provider.session_header:
        headers[provider.session_header] = session
    return headers


def slot_name(model: dict | str) -> str:
    """The directory that holds one model's results. A bare model ID is an OpenCode Go model."""
    if isinstance(model, str):
        return f"{GO.id}-{model}"
    return f"{provider_of(model).id}-{model['id']}"


def run_label(model: dict | str, track: str) -> str:
    """How a model's runs are labelled in records, reports and the site."""
    if isinstance(model, str):
        return f"{GO.id}/{model} [{track}]"
    return f"{provider_of(model).id}/{model['id']} [{track}]"


def source_name(plan: dict) -> str:
    """What a dataset's models were called through, for its catalog label."""
    names = sorted({provider_of(model).name for model in plan.get("models", [])}) or [GO.name]
    return names[0] if len(names) == 1 else "Direct APIs"


def billing_modes(plan: dict) -> set[str]:
    return {provider_of(model).billing for model in plan["models"]}


def check_billing(plan: dict, *, subscription_only: bool, metered: bool) -> None:
    """A live dispatch must acknowledge exactly how the plan's requests are paid for.

    `subscription_only` confirms that Go "Use balance" is off. `metered`
    confirms that the plan calls pay-as-you-go APIs, where every request is
    billed per token. Raises ValueError unless the two match the plan.
    """
    modes = billing_modes(plan)
    if ("subscription" in modes) != subscription_only:
        raise ValueError(
            "--subscription-only is required for OpenCode Go models (Use balance must be OFF) "
            "and must be left out when the plan has none"
        )
    if ("metered" in modes) != metered:
        raise ValueError(
            "--metered is required for pay-as-you-go providers (billed per token) "
            "and must be left out when the plan has none"
        )
