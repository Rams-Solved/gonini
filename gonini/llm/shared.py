"""Prompt text and payload shaping shared by the real LLM providers.

``AnthropicClient`` and ``OpenRouterClient`` are fenced the same way and talk
about the same facts — this keeps the system prompt, JSON-only nudge, schemas,
and payload shaping in one place instead of drifting between two copies.
"""

from __future__ import annotations

import sys

from ..models import ExceptionRecord

SYSTEM_PROMPT = (
    "You are gonini's drafting assistant for a Fulfilment-as-a-Service operations "
    "team. A deterministic rules engine has already detected, classified, and "
    "priced every exception. Your job is strictly to phrase things for a human "
    "reviewer.\n"
    "RULES:\n"
    "- Use ONLY the facts provided in the request. Never invent or alter order "
    "ids, amounts, quantities, timestamps, counts, or exception types.\n"
    "- Do not recompute or 'correct' any number.\n"
    "- Every outbound artefact is a DRAFT pending human approval; write in that "
    "spirit — clear, factual, no false certainty.\n"
    "- Keep it concise and professional."
)

# Used to retry a failed JSON parse once, with a stricter instruction, before
# giving up and falling back to templates.
JSON_ONLY_NUDGE = (
    "Return ONLY valid JSON matching the requested shape. No markdown code "
    "fences, no leading or trailing prose, no explanation — the entire "
    "response must be a single parseable JSON object."
)

ROOT_CAUSE_SCHEMA = {
    "type": "object",
    "properties": {"causes": {"type": "array", "items": {"type": "string"}}},
    "required": ["causes"],
    "additionalProperties": False,
}

EMAIL_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}


def items_payload(items: list[ExceptionRecord]) -> list[dict]:
    return [
        {
            "order_id": e.order_id,
            "type": e.type.value,
            "title": e.title,
            "severity": e.severity.label,
            "detail": e.detail,
            "evidence": e.evidence,
            "responsible_party": e.responsible_party,
        }
        for e in items
    ]


def warn(where: str, exc: Exception) -> None:
    print(
        f"[gonini] LLM call '{where}' failed ({exc}); using template fallback.",
        file=sys.stderr,
    )
