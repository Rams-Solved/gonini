"""OpenRouter LLM implementation (OpenAI-compatible chat completions).

Used when OpenRouter is the selected provider (see ``llm/__init__.py`` for
provider selection order). Talks to OpenRouter's chat completions endpoint
over stdlib ``urllib`` — no extra SDK dependency. Default model is
``openrouter/free``, OpenRouter's auto-router across live free models;
overridable via ``--model``.

Free-tier models are noticeably weaker at strict JSON than Sonnet, so JSON
responses go through tolerant extraction (strip markdown fences, tolerate
stray prose — see ``json_utils.py``) and, on parse failure, one retry with an
explicit "JSON only" nudge before giving up. Transient failures (429/5xx) are
retried with backoff first; if all of that still fails, we fall back to the
offline templates so a run never dies on it.

OpenRouter's free tier is rate-limited to roughly 50 requests/day and 20/min
per key — see the README for details.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime

from .. import config
from ..models import ExceptionRecord
from ..util import fmt_ts
from .base import DigestFacts, EmailDraft, LLMClient
from .json_utils import extract_json
from .mock import MockClient
from .retry import RetryableError, call_with_retry
from .shared import JSON_ONLY_NUDGE, SYSTEM_PROMPT, items_payload, warn

_ENDPOINT = f"{config.OPENROUTER_BASE_URL}/chat/completions"


class OpenRouterClient(LLMClient):
    """Fenced LLM client backed by OpenRouter's chat completions API."""

    def __init__(self, model: str = config.OPENROUTER_MODEL) -> None:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        self._api_key = api_key
        self._model = model
        self._fallback = MockClient()
        self.name = f"openrouter:{model}"

    # -- low-level helpers ------------------------------------------------- #
    def _complete(self, messages: list[dict]) -> str:
        body = json.dumps(
            {
                "model": self._model,
                "messages": messages,
                "max_tokens": config.LLM_MAX_TOKENS,
            }
        ).encode("utf-8")

        def call() -> str:
            req = urllib.request.Request(
                _ENDPOINT,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/gonini/gonini",
                    "X-Title": "gonini",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    raise RetryableError(f"HTTP {exc.code}") from exc
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"OpenRouter HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                # DNS failure, connection refused, timeout, etc. — worth a retry.
                raise RetryableError(str(exc)) from exc

            choices = payload.get("choices") or []
            if not choices:
                raise RuntimeError(f"OpenRouter returned no choices: {payload}")
            return choices[0]["message"]["content"] or ""

        return call_with_retry(call, attempts=config.LLM_RETRY_ATTEMPTS)

    def _text(self, user: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        return self._complete(messages).strip()

    def _json(self, user: str) -> dict:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        raw = self._complete(messages)
        try:
            return extract_json(raw)
        except ValueError:
            # Free-tier models are weaker at strict JSON — one retry with an
            # explicit nudge before giving up and falling back to templates.
            nudged = messages + [{"role": "user", "content": JSON_ONLY_NUDGE}]
            raw2 = self._complete(nudged)
            return extract_json(raw2)

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
                "likely root cause, grounded in its evidence. Return ONLY a JSON "
                'object of the shape {"causes": [string, ...]} with exactly one '
                "string per exception, in the same order — no markdown, no prose "
                "outside the JSON.\n\n" + json.dumps(items_payload(items), indent=2)
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
                "Draft an escalation email to this responsible party. Return ONLY "
                'a JSON object of the shape {"subject": string, "body": string} '
                "— body is markdown. Include every exception with its evidence and "
                "suggested action. Keep all ids, amounts, and timestamps exactly as "
                "given — no markdown fences, no prose outside the JSON.\n\n"
                + json.dumps(payload, indent=2)
            )
            return EmailDraft(subject=data["subject"], body=data["body"])
        except Exception as exc:  # noqa: BLE001
            warn("draft_email", exc)
            return self._fallback.draft_email(party, party_kind, items, as_of)
