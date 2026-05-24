"""OpenAI-compatible LLM client.

Works with OpenAI, Azure OpenAI, and any OpenAI-compatible endpoint — including
local inference servers (Ollama, llama.cpp) — via the ``base_url`` override.
"""

from __future__ import annotations

import os

from .base import LLMClient, LLMError

DEFAULT_MODEL = "gpt-4o"
MAX_TOKENS = 8192


class OpenAIClient(LLMClient):
    """An OpenAI-compatible chat client.

    ``api_key`` may be left empty for local servers (Ollama / llama.cpp) that
    don't authenticate — the SDK still requires the field to be non-empty, so a
    placeholder is supplied automatically when a ``base_url`` is set.
    """

    def __init__(
        self,
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        name: str = "openai",
    ):
        self._model = model or DEFAULT_MODEL
        self._base_url = base_url
        self._api_key = api_key
        self.name = name

    @property
    def model_id(self) -> str:
        return self._model

    def _client(self):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "openai SDK not installed — run: pip install 'poddebugger[openai]'"
            ) from exc
        kwargs: dict = {}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        if self._api_key:
            kwargs["api_key"] = self._api_key
        elif self._base_url and not os.environ.get("OPENAI_API_KEY"):
            # Local inference servers don't need a key, but the SDK rejects
            # an empty one — supply a harmless placeholder.
            kwargs["api_key"] = "local"
        return openai, openai.OpenAI(**kwargs)

    def complete(self, system: str, user: str) -> str:
        openai, client = self._client()
        try:
            resp = client.chat.completions.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except openai.AuthenticationError as exc:
            raise LLMError(f"{self.name} auth failed — check your API key") from exc
        except openai.APIConnectionError as exc:
            raise LLMError(
                f"could not reach {self.name} at {self._base_url or 'the API'} "
                f"— is the server running? ({exc})"
            ) from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"{self.name} API error: {exc}") from exc

        text = resp.choices[0].message.content or ""
        if not text.strip():
            raise LLMError(f"{self.name} returned an empty response")
        return text
