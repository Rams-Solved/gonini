"""Real LLM implementation backed by the Anthropic Messages API.

Used when Anthropic is the selected provider (see ``llm/__init__.py`` for
provider selection order). The model is handed the deterministic facts and
evidence and asked to *phrase* them — the system prompt forbids inventing
order ids, amounts, timestamps, or counts. Transient failures (429/5xx) are
retried with backoff; if a call still fails, we fall back to the offline
templates so a run never dies on a transient API error. The SDK is imported
lazily so the package has no hard dependency on ``anthropic``.
"""

from __future__ import annotations

import json
from datetime import datetime

from .. import config
from ..models import ExceptionRecord
from ..util import fmt_ts
from .base import DigestFacts, EmailDraft, LLMClient
from .json_utils import extract_json
from .mock import MockClient
from .retry import RetryableError, call_with_retry
from .shared import EMAIL_SCHEMA, ROOT_CAUSE_SCHEMA, SYSTEM_PROMPT, items_payload, warn


class AnthropicClient(LLMClient):
    """Fenced LLM client. Prose in, prose out; numbers are never the model's job."""

    def __init__(self, model: str = config.ANTHROPIC_MODEL) -> None:
        import anthropic  # lazy: only needed on the real path

        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self._model = model
        self._fallback = MockClient()
        self.name = f"anthropic:{model}"

    # -- low-level helpers ------------------------------------------------- #
    def _create(self, **kwargs):
        def call():
            try:
                return self._client.messages.create(**kwargs)
            except self._anthropic.RateLimitError as exc:
                raise RetryableError(str(exc)) from exc
            except self._anthropic.APIStatusError as exc:
                if exc.status_code >= 500:
                    raise RetryableError(str(exc)) from exc
                raise

        return call_with_retry(call, attempts=config.LLM_RETRY_ATTEMPTS)

    def _text(self, user: str) -> str:
        resp = self._create(
            model=self._model,
            max_tokens=config.LLM_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def _json(self, user: str, schema: dict) -> dict:
        resp = self._create(
            model=self._model,
            max_tokens=config.LLM_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next(b.text for b in resp.content if b.type == "text")
        return extract_json(text)

    # -- interface --------------------------------------------------------- #
    def headline(self, facts: DigestFacts) -> str:
        payload = {
            "orders_reconciled": facts.orders_reconciled,
            "exceptions": facts.exception_count,
            "affected_orders": facts.affected_orders,
            "seller_facing": facts.seller_facing,
            "money": facts.money,
            "hygiene": facts.hygiene,
            "recoverable": facts.recoverable,
            "top_types": facts.top_types,
            "worst_warehouse": facts.worst_warehouse,
            "worst_carrier": facts.worst_carrier,
        }
        try:
            return self._text(
                "Write a 2-3 sentence morning summary for the ops lead using ONLY "
                "these facts. Lead with what needs attention. Do not add numbers "
                "not present here.\n\n" + json.dumps(payload, indent=2)
            )
        except Exception as exc:  # noqa: BLE001 - resilience over strictness
            warn("headline", exc)
            return self._fallback.headline(facts)

    def root_causes(self, items: list[ExceptionRecord]) -> list[str]:
        if not items:
            return []
        try:
            data = self._json(
                "For each exception below, give ONE short sentence naming the most "
                "likely root cause, grounded in its evidence. Return a 'causes' "
                "array with exactly one string per exception, in the same order.\n\n"
                + json.dumps(items_payload(items), indent=2),
                ROOT_CAUSE_SCHEMA,
            )
            causes = list(data.get("causes", []))
            if len(causes) != len(items):  # keep alignment guarantees
                raise ValueError("root-cause count mismatch")
            return causes
        except Exception as exc:  # noqa: BLE001
            warn("root_causes", exc)
            return self._fallback.root_causes(items)

    def draft_email(
        self,
        party: str,
        party_kind: str,
        items: list[ExceptionRecord],
        as_of: datetime,
    ) -> EmailDraft:
        payload = {
            "party": party,
            "party_kind": party_kind,
            "as_of": fmt_ts(as_of),
            "exceptions": items_payload(items),
            "recoverable_total_pence": sum(e.recoverable_pence for e in items),
        }
        try:
            data = self._json(
                "Draft an escalation email to this responsible party. Markdown "
                "body. Include every exception with its evidence and suggested "
                "action. Keep all ids, amounts, and timestamps exactly as given.\n\n"
                + json.dumps(payload, indent=2),
                EMAIL_SCHEMA,
            )
            return EmailDraft(subject=data["subject"], body=data["body"])
        except Exception as exc:  # noqa: BLE001
            warn("draft_email", exc)
            return self._fallback.draft_email(party, party_kind, items, as_of)
