"""What a run cost at list prices, for models called through pay-as-you-go providers.

A generation receipt records `billable_tokens`: provider-reported counts in
the four classes providers price separately (uncached input, cache reads,
cache writes, output). Cost is those counts times the list prices below. It
is an estimate from published prices, not an invoice.

Only the base price tier is used. OpenAI and xAI charge more for a request
whose context is over 272,000 and 200,000 tokens; an agent-assisted slot's
whole conversation is capped at 1.5 MB, so few requests can reach that.

OpenCode Go models have no entry: a subscription has no per-token price, so
their cost is unknown rather than zero.
"""

from __future__ import annotations

PRICES_CHECKED_AT = "2026-11-09"
PRICE_SOURCES = {
    "default": "https://models.dev/api.json",
    # Not on models.dev yet. Input, output and cache reads are Anthropic's
    # published rates; the cache-write rate is its standard 1.25x of input.
    "anthropic/claude-fable-5-2": "https://www.anthropic.com/pricing",
}

# USD per million tokens, keyed by "<provider>/<model>" as in a run label.
PRICES = {
    "anthropic/claude-fable-5-1": {"input": 10.0, "cache_read": 0.25, "cache_write": 12.5, "output": 50.0},
    "anthropic/claude-fable-5-2": {"input": 8.0, "cache_read": 0.20, "cache_write": 10.0, "output": 40.0},
    "openai/gpt-6-luna": {"input": 0.10, "cache_read": 0.01, "cache_write": 0.125, "output": 0.50},
    "xai/grok-4.7": {"input": 2.0, "cache_read": 0.50, "cache_write": 2.0, "output": 6.0},
    "moonshot/kimi-k3": {"input": 3.0, "cache_read": 0.30, "cache_write": 3.0, "output": 15.0},
}
TOKEN_CLASSES = ("input", "cache_read", "cache_write", "output")


def model_key(label: str) -> str:
    """ "anthropic/claude-fable-5-1 [agent-assisted-v2]" -> "anthropic/claude-fable-5-1" """
    return label.split(" [", 1)[0]


def cost_usd(tokens: dict, price: dict) -> float:
    return sum(tokens[name] * price[name] for name in TOKEN_CLASSES) / 1_000_000


def run_cost(label: str, generation: dict) -> float | None:
    """The list-price cost of one run, or None when it has no price or no token receipt."""
    price = PRICES.get(model_key(label))
    tokens = generation.get("billable_tokens")
    if price is None or not isinstance(tokens, dict):
        return None
    if any(type(tokens.get(name)) is not int or tokens[name] < 0 for name in TOKEN_CLASSES):
        return None
    return cost_usd(tokens, price)
