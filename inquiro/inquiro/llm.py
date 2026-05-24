"""The LLM client interface.

Clients are deliberately thin: they take a system + user prompt and return
raw text. Prompt construction and response parsing live in the application,
so every provider behaves identically. Concrete clients (Anthropic, OpenAI,
Ollama, llama.cpp, ...) live in the applications that use them.
"""

from __future__ import annotations

import abc


class LLMError(RuntimeError):
    """Raised when an LLM call cannot be made or fails."""


class LLMClient(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Return the model's text response to (system, user)."""

    @property
    @abc.abstractmethod
    def model_id(self) -> str:
        """The resolved model identifier, for display."""
