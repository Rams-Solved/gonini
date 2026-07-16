"""Template-based fallback that satisfies :class:`LLMClient` without a key.

Every method here is deterministic and offline. It is what runs in
``--no-llm`` mode (and whenever no API key is present), so ``gonini demo``
produces a complete digest and outbox with zero external dependencies. The
prose is intentionally plain — the point is that the *structure and numbers*
are identical whether or not a real model is in the loop.
"""

from __future__ import annotations

from datetime import datetime

from ..models import ExceptionRecord
from ..taxonomy import ExceptionType
from ..util import fmt_ts
from .base import DigestFacts, EmailDraft, LLMClient

# A plausible, evidence-agnostic root-cause line per taxonomy type.
_ROOT_CAUSE = {
    ExceptionType.MISSING_IN_WMS: "Order likely dropped at the platform→WMS handoff (routing or integration gap).",
    ExceptionType.STUCK_NO_MOVEMENT: "Pick queue backlog or a stock/allocation block holding the order at this stage.",
    ExceptionType.DESPATCHED_NOT_SCANNED: "Despatch marked in the WMS but the parcel missed the carrier collection or manifest scan.",
    ExceptionType.TRACKING_STALLED: "Parcel likely mis-sorted or held in the carrier network with no scan event.",
    ExceptionType.QTY_MISMATCH: "Short pick or a split shipment not reflected back to the order line.",
    ExceptionType.DELIVERED_NOT_CLOSED: "Delivery webhook not consumed, so the platform never advanced the order to closed.",
    ExceptionType.SLA_DESPATCH_BREACH: "Cut-off missed — likely late intake, capacity, or a stock wait pushing despatch past the SLA.",
    ExceptionType.INVOICE_OVERBILL: "Rate applied above the contracted rate card, or a stale price in the billing feed.",
    ExceptionType.INVOICE_PHANTOM: "Billing line generated with no fulfilment activity behind it.",
    ExceptionType.INVOICE_DUPLICATE: "Same fulfilment event billed twice — a re-run or double-post in the billing feed.",
}


class MockClient(LLMClient):
    name = "mock (template)"

    def headline(self, facts: DigestFacts) -> str:
        lead = (
            f"{facts.exception_count} exception(s) across {facts.affected_orders} "
            f"order(s) need attention this morning "
            f"({facts.seller_facing} seller-facing, {facts.money} money, "
            f"{facts.hygiene} hygiene)."
        )
        money = (
            f" {facts.recoverable} is recoverable against the rate card."
            if facts.recoverable != "£0.00"
            else ""
        )
        focus = ""
        if facts.worst_warehouse:
            focus += f" Watch {facts.worst_warehouse} on despatch SLA."
        if facts.worst_carrier:
            focus += f" {facts.worst_carrier} has stalled tracking to chase."
        return lead + money + focus

    def root_causes(self, items: list[ExceptionRecord]) -> list[str]:
        return [_ROOT_CAUSE.get(e.type, "Investigate the flagged divergence.") for e in items]

    def draft_email(
        self,
        party: str,
        party_kind: str,
        items: list[ExceptionRecord],
        as_of: datetime,
    ) -> EmailDraft:
        recoverable = sum(e.recoverable_pence for e in items)
        subject = f"[gonini] {len(items)} reconciliation exception(s) for {party} — {fmt_ts(as_of)[:10]}"
        lines = [
            f"Hi {party} team,",
            "",
            (
                f"This morning's automated reconciliation flagged {len(items)} "
                f"exception(s) owned by {party}. Details and supporting evidence "
                "are below for your review."
            ),
            "",
        ]
        for e in items:
            lines.append(f"- **{e.order_id} — {e.title}** ({e.severity.label})")
            lines.append(f"  - {e.detail}")
            lines.append(f"  - Suggested action: {e.action}")
            lines.append(f"  - Deadline: {fmt_ts(e.deadline())[:10]}")
            for k, v in e.evidence.items():
                if k == "lines":
                    for ln in v:
                        lines.append(f"  - line: {ln}")
                else:
                    lines.append(f"  - {k}: {v}")
            lines.append("")
        if recoverable > 0:
            lines.append(f"Estimated recoverable amount: £{recoverable / 100:,.2f}.")
            lines.append("")
        lines.append("Please confirm remediation or reply with questions.")
        lines.append("")
        lines.append("— gonini (order exception reconciliation agent)")
        return EmailDraft(subject=subject, body="\n".join(lines))
