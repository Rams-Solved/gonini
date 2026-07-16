"""Tolerant JSON extraction for LLM responses.

Free-tier / weaker models are noticeably less reliable at strict JSON than
Sonnet: they wrap the object in markdown fences, add a sentence of prose
before or after it, or both. This does light, safe cleanup only — it never
repairs semantically wrong JSON, it just finds the JSON that's actually
there.
"""

from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> dict:
    """Parse ``text`` as a JSON object, tolerating fences and stray prose.

    Raises ``ValueError`` if no parseable JSON object can be found.
    """
    text = text.strip()

    fence = _FENCE_RE.search(text)
    candidate = fence.group(1).strip() if fence else text

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost {...} span — tolerates leading/trailing prose
    # the model added around the object despite being asked not to.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"could not extract JSON from response: {text[:200]!r}")
