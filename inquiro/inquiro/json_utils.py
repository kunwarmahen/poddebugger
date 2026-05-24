"""JSON-from-LLM extraction helpers.

Recover a JSON object from an LLM's response — tolerant of code fences and
of prefix/suffix prose. Used by every agent that expects a JSON payload back.
"""

from __future__ import annotations

import json
import re

from .llm import LLMError

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> dict:
    """Pull the JSON object out of an LLM response, tolerating code fences.

    Raises :class:`LLMError` if no object can be recovered.
    """
    cleaned = _FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back to the first {...} span.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                pass
    raise LLMError("could not parse a JSON object from the LLM response")
