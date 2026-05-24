"""Anthropic Claude LLM client.

Single-turn ``messages.create`` with a static (cacheable) system prompt and a
dynamic user prompt. Adaptive thinking is enabled since root-cause analysis is
a genuine reasoning task.
"""

from __future__ import annotations

from .base import LLMClient, LLMError

DEFAULT_MODEL = "claude-opus-4-7"
MAX_TOKENS = 8192


class AnthropicClient(LLMClient):
    name = "anthropic"

    def __init__(self, model: str = "", base_url: str = ""):
        self._model = model or DEFAULT_MODEL
        self._base_url = base_url

    @property
    def model_id(self) -> str:
        return self._model

    def _client(self):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "anthropic SDK not installed — run: pip install 'poddebugger[anthropic]'"
            ) from exc
        kwargs = {}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return anthropic, anthropic.Anthropic(**kwargs)

    def complete(self, system: str, user: str) -> str:
        anthropic, client = self._client()
        try:
            resp = client.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                # System prompt is static across invocations -> mark it cacheable.
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic auth failed — check ANTHROPIC_API_KEY") from exc
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic API error: {exc}") from exc

        text = "".join(b.text for b in resp.content if b.type == "text")
        if not text.strip():
            raise LLMError("Anthropic returned an empty response")
        return text
