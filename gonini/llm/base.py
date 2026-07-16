"""The LLM boundary.

The rules engine has already done every calculation by the time anything here
runs. The :class:`LLMClient` interface is deliberately narrow — the model may
only (a) narrate facts it is handed, (b) infer a plausible root cause for an
already-classified exception, and (c) draft escalation prose. It never
classifies into the taxonomy, never computes a number, and never decides
priority. That fence is what makes the pipeline auditable; see the README.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime

from ..models import ExceptionRecord


@dataclass
class DigestFacts:
    """Everything the narrative layer is allowed to talk about — all of it
    computed deterministically upstream. The LLM phrases these; it does not
    produce or alter them."""

    as_of: datetime
    orders_reconciled: int
    exception_count: int
    affected_orders: int
    seller_facing: int
    money: int
    hygiene: int
    recoverable: str  # pre-formatted £ string
    top_types: list[tuple[str, int]] = field(default_factory=list)  # (label, count)
    worst_warehouse: str = ""
    worst_carrier: str = ""


@dataclass
class EmailDraft:
    subject: str
    body: str


class LLMClient(abc.ABC):
    """Interface both the mock and the real Anthropic client implement."""

    #: Short label shown in the digest so a reader knows what produced the prose.
    name: str = "llm"

    @abc.abstractmethod
    def headline(self, facts: DigestFacts) -> str:
        """A 2–3 sentence morning summary of the run. Prose only."""

    @abc.abstractmethod
    def root_causes(self, items: list[ExceptionRecord]) -> list[str]:
        """A one-line root-cause hypothesis per exception, order-aligned with
        ``items``. Advisory — grounded in the evidence, not authoritative."""

    @abc.abstractmethod
    def draft_email(
        self,
        party: str,
        party_kind: str,
        items: list[ExceptionRecord],
        as_of: datetime,
    ) -> EmailDraft:
        """Draft an escalation email to one responsible party."""
