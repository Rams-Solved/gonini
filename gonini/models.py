"""Typed records passed between the engine, the store, and the LLM layer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .taxonomy import Category, ExceptionType, Severity, meta


@dataclass
class ExceptionRecord:
    """A single flagged divergence between the systems of record.

    Every field here is produced deterministically by the rules engine. The
    ``evidence`` dict carries the raw conflicting rows so a human (or the LLM)
    can see exactly why the flag was raised without re-querying the sources.
    """

    order_id: str
    type: ExceptionType
    responsible_party: str
    detail: str
    evidence: dict = field(default_factory=dict)
    recoverable_pence: int = 0
    detected_at: datetime = None  # type: ignore[assignment]

    # --- derived, from the taxonomy -------------------------------------- #
    @property
    def category(self) -> Category:
        return meta(self.type).category

    @property
    def severity(self) -> Severity:
        return meta(self.type).severity

    @property
    def title(self) -> str:
        return meta(self.type).title

    @property
    def action(self) -> str:
        return meta(self.type).action

    def deadline(self) -> datetime:
        base = self.detected_at or datetime.now()
        return base + timedelta(days=self.severity.deadline_days)

    @property
    def sort_key(self) -> tuple:
        """Deterministic priority order: seller-facing > money > hygiene,
        then severity, then largest recoverable amount, then order id."""
        return (
            self.category.rank,
            self.severity.rank,
            -self.recoverable_pence,
            self.order_id,
        )

    def evidence_json(self) -> str:
        return json.dumps(self.evidence, sort_keys=True, default=str)


@dataclass
class WarehouseSLA:
    """Despatch-SLA attainment for one fulfilment site."""

    warehouse_id: str
    despatched: int
    on_time: int

    @property
    def breached(self) -> int:
        return self.despatched - self.on_time

    @property
    def attainment_pct(self) -> float:
        if self.despatched == 0:
            return 100.0
        return round(100.0 * self.on_time / self.despatched, 1)


@dataclass
class CarrierSLA:
    """Delivery-health attainment for one carrier."""

    carrier_id: str
    shipments: int
    stalled: int

    @property
    def healthy(self) -> int:
        return self.shipments - self.stalled

    @property
    def attainment_pct(self) -> float:
        if self.shipments == 0:
            return 100.0
        return round(100.0 * self.healthy / self.shipments, 1)


@dataclass
class InvoiceRecovery:
    """Rate-card recomputation: expected vs billed, and what is recoverable."""

    expected_pence: int
    billed_pence: int

    @property
    def recoverable_pence(self) -> int:
        return max(0, self.billed_pence - self.expected_pence)


@dataclass
class ReconResult:
    """The full deterministic output of a reconciliation run."""

    orders_reconciled: int
    exceptions: list[ExceptionRecord]
    warehouse_sla: list[WarehouseSLA]
    carrier_sla: list[CarrierSLA]
    invoice_recovery: InvoiceRecovery
    as_of: datetime

    def by_category(self) -> dict[Category, int]:
        counts = {c: 0 for c in Category}
        for e in self.exceptions:
            counts[e.category] += 1
        return counts

    def top(self, n: int = 10) -> list[ExceptionRecord]:
        return sorted(self.exceptions, key=lambda e: e.sort_key)[:n]

    @property
    def affected_orders(self) -> int:
        return len({e.order_id for e in self.exceptions})
