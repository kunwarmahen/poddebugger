"""Pluggable LLM clients."""

from __future__ import annotations

from .base import LLMClient, LLMError

# Local OpenAI-compatible inference servers: default endpoint + default model.
_LOCAL_SERVERS = {
    "ollama": ("http://localhost:11434/v1", "llama3.1"),
    "llamacpp": ("http://localhost:8080/v1", "local-model"),
}


def get_llm(provider: str, model: str = "", base_url: str = "") -> LLMClient:
    """Factory: return the LLM client for the configured provider.

    Supported: ``anthropic``, ``openai`` (and OpenAI-compatible gateways),
    ``ollama``, ``llamacpp`` (local inference servers).
    """
    provider = (provider or "anthropic").lower()

    if provider == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(model=model, base_url=base_url)

    if provider in ("openai", "azure", "vllm", "openai-compatible"):
        from .openai_client import OpenAIClient

        return OpenAIClient(model=model, base_url=base_url, name=provider)

    if provider in ("ollama", "llamacpp", "llama.cpp", "llama-cpp"):
        from .openai_client import OpenAIClient

        key = "ollama" if provider == "ollama" else "llamacpp"
        default_url, default_model = _LOCAL_SERVERS[key]
        return OpenAIClient(
            model=model or default_model,
            base_url=base_url or default_url,
            api_key="local",
            name=key,
        )

    raise LLMError(
        f"unknown LLM provider: {provider!r} "
        "(expected anthropic, openai, ollama, or llamacpp)"
    )


__all__ = ["LLMClient", "LLMError", "get_llm"]
