"""The exception taxonomy: the closed vocabulary the rules engine flags into.

The taxonomy is deliberately fixed and code-owned. The deterministic engine
maps every divergence onto exactly one :class:`ExceptionType`; the LLM layer
may narrate these but may never invent new ones. Severity is rule-based and
follows the ordering principle **seller-facing > money > hygiene**.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Category(Enum):
    """Coarse impact bucket. Rank drives prioritisation order."""

    SELLER_FACING = 0  # a seller or their customer feels this directly
    MONEY = 1  # cash leaking out or mis-billed
    HYGIENE = 2  # data cleanliness; no external impact

    @property
    def rank(self) -> int:
        return self.value

    @property
    def label(self) -> str:
        return {
            Category.SELLER_FACING: "Seller-facing",
            Category.MONEY: "Money",
            Category.HYGIENE: "Hygiene",
        }[self]


class Severity(Enum):
    """Rule-based severity. Lower rank == more urgent."""

    CRITICAL = 0
    HIGH = 1
    MEDIUM = 2
    LOW = 3

    @property
    def rank(self) -> int:
        return self.value

    @property
    def label(self) -> str:
        return self.name.title()

    # Days to add to the run date to produce a remediation deadline.
    @property
    def deadline_days(self) -> int:
        return {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 3,
            Severity.LOW: 5,
        }[self]


class ExceptionType(Enum):
    """The ten reconciliation failure modes gonini knows how to flag."""

    MISSING_IN_WMS = "MISSING_IN_WMS"
    STUCK_NO_MOVEMENT = "STUCK_NO_MOVEMENT"
    DESPATCHED_NOT_SCANNED = "DESPATCHED_NOT_SCANNED"
    TRACKING_STALLED = "TRACKING_STALLED"
    QTY_MISMATCH = "QTY_MISMATCH"
    DELIVERED_NOT_CLOSED = "DELIVERED_NOT_CLOSED"
    SLA_DESPATCH_BREACH = "SLA_DESPATCH_BREACH"
    INVOICE_OVERBILL = "INVOICE_OVERBILL"
    INVOICE_PHANTOM = "INVOICE_PHANTOM"
    INVOICE_DUPLICATE = "INVOICE_DUPLICATE"


@dataclass(frozen=True)
class TypeMeta:
    """Static metadata attached to each exception type."""

    category: Category
    severity: Severity
    title: str
    #: One-line, evidence-grounded remediation the ops team should take.
    action: str
    #: Which side of the operation owns the fix (used to route escalations).
    owner_role: str


# The single source of truth mapping each type to its impact and playbook.
TYPE_META: dict[ExceptionType, TypeMeta] = {
    ExceptionType.MISSING_IN_WMS: TypeMeta(
        Category.SELLER_FACING,
        Severity.CRITICAL,
        "Order never reached the warehouse",
        "Manually inject the order into the WMS and confirm stock allocation today.",
        "Warehouse intake",
    ),
    ExceptionType.STUCK_NO_MOVEMENT: TypeMeta(
        Category.SELLER_FACING,
        Severity.HIGH,
        "Order stalled inside the warehouse",
        "Chase the pick/pack queue and progress the order to despatch.",
        "Warehouse operations",
    ),
    ExceptionType.DESPATCHED_NOT_SCANNED: TypeMeta(
        Category.SELLER_FACING,
        Severity.HIGH,
        "Marked despatched but no carrier handover",
        "Confirm the parcel physically left the building and reconcile the manifest.",
        "Warehouse operations",
    ),
    ExceptionType.TRACKING_STALLED: TypeMeta(
        Category.SELLER_FACING,
        Severity.HIGH,
        "Carrier tracking has gone quiet",
        "Open a carrier trace and pre-empt the customer with an update.",
        "Carrier account",
    ),
    ExceptionType.QTY_MISMATCH: TypeMeta(
        Category.MONEY,
        Severity.MEDIUM,
        "Quantity shipped differs from the order",
        "Reconcile the pick against the order line and issue a make-good or credit.",
        "Warehouse operations",
    ),
    ExceptionType.DELIVERED_NOT_CLOSED: TypeMeta(
        Category.HYGIENE,
        Severity.LOW,
        "Delivered but the order is still open",
        "Close the order on the platform so the seller stops chasing it.",
        "Platform operations",
    ),
    ExceptionType.SLA_DESPATCH_BREACH: TypeMeta(
        Category.SELLER_FACING,
        Severity.HIGH,
        "Despatched after the ship-by SLA",
        "Log the breach against the site scorecard and notify the seller of the delay.",
        "Warehouse operations",
    ),
    ExceptionType.INVOICE_OVERBILL: TypeMeta(
        Category.MONEY,
        Severity.HIGH,
        "Billed above the rate card",
        "Raise a billing dispute for the delta and withhold payment on the line.",
        "Warehouse billing",
    ),
    ExceptionType.INVOICE_PHANTOM: TypeMeta(
        Category.MONEY,
        Severity.HIGH,
        "Invoice line with no matching order",
        "Reject the line: there is no fulfilment activity to support it.",
        "Warehouse billing",
    ),
    ExceptionType.INVOICE_DUPLICATE: TypeMeta(
        Category.MONEY,
        Severity.HIGH,
        "Same fulfilment billed twice",
        "Reject the duplicate line and reclaim the double charge.",
        "Warehouse billing",
    ),
}


def meta(t: ExceptionType) -> TypeMeta:
    """Return the static metadata for an exception type."""
    return TYPE_META[t]
