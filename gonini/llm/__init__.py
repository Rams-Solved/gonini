"""LLM layer factory.

Provider selection order, unless overridden by an explicit ``--provider``
flag: ``OPENROUTER_API_KEY`` beats ``ANTHROPIC_API_KEY`` beats the offline
templates. ``--no-llm`` always forces the templates. Both real providers
retry transient (429/5xx) failures with backoff before giving up, and fall
back to the mock on any other failure, so a run never dies on it.
"""

from __future__ import annotations

import importlib.util
import os

from .. import config
from .base import DigestFacts, EmailDraft, LLMClient
from .mock import MockClient

__all__ = ["DigestFacts", "EmailDraft", "LLMClient", "MockClient", "get_client"]

PROVIDERS = ("auto", "anthropic", "openrouter", "mock")


def get_client(
    no_llm: bool, model: str | None = None, provider: str = "auto"
) -> tuple[LLMClient, str]:
    """Return an ``(client, mode_note)`` pair.

    ``--no-llm`` forces the mock regardless of ``provider``. Otherwise
    ``provider`` picks the backend explicitly ("openrouter" / "anthropic" /
    "mock"), or "auto" (the default) resolves it from environment variables:
    ``OPENROUTER_API_KEY`` first, then ``ANTHROPIC_API_KEY``, then the mock.
    If a provider is picked (explicitly or via auto-resolution) but its
    prerequisites aren't met, we fall back to the mock and say why.
    """
    if no_llm:
        return MockClient(), "mock (--no-llm)"
    if provider == "mock":
        return MockClient(), "mock (--provider mock)"

    resolved = provider
    if resolved == "auto":
        if os.environ.get("OPENROUTER_API_KEY"):
            resolved = "openrouter"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            resolved = "anthropic"
        else:
            return MockClient(), "mock (auto-fallback: no API key set)"

    if resolved == "openrouter":
        if not os.environ.get("OPENROUTER_API_KEY"):
            return MockClient(), "mock (auto-fallback: no OPENROUTER_API_KEY)"
        from .openrouter_client import OpenRouterClient

        m = model or config.OPENROUTER_MODEL
        return OpenRouterClient(m), f"openrouter:{m}"

    if resolved == "anthropic":
        has_sdk = importlib.util.find_spec("anthropic") is not None
        if not has_sdk:
            return MockClient(), "mock (auto-fallback: no anthropic SDK)"
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return MockClient(), "mock (auto-fallback: no ANTHROPIC_API_KEY)"
        from .anthropic_client import AnthropicClient

        m = model or config.ANTHROPIC_MODEL
        return AnthropicClient(m), f"anthropic:{m}"

    raise ValueError(f"unknown provider {resolved!r}; choose one of {PROVIDERS}")
