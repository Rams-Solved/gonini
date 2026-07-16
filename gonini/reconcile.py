"""The deterministic rules engine.

This module does **all** diffing and flagging. It joins the three systems of
record (plus the invoice and rate card) on ``order_id`` and maps every
divergence onto exactly one taxonomy type using explicit, auditable rules —
no model, no probability, no hallucination surface. Its output is the
exception table, per-warehouse and per-carrier SLA attainment, and the invoice
recovery total, all persisted to SQLite.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config, db
from .models import (
    CarrierSLA,
    ExceptionRecord,
    InvoiceRecovery,
    ReconResult,
    WarehouseSLA,
)
from .taxonomy import ExceptionType
from .util import fmt_ts, hours_between, parse_ts, to_pence

# Terminal platform statuses that count as "closed" for hygiene checks.
_CLOSED_STATUSES = {"CLOSED", "DELIVERED", "CANCELLED"}
_WMS_ORDER = ("received", "picked", "packed", "despatched")


# --------------------------------------------------------------------------- #
# Parsed, per-order views of each source system
# --------------------------------------------------------------------------- #
@dataclass
class _Order:
    order_id: str
    seller_id: str
    created_at: datetime
    qty: int
    status: str
    ship_by: datetime


@dataclass
class _WmsView:
    warehouse_id: str
    stamps: dict  # event -> datetime
    qty_by_event: dict  # event -> qty
    last_event: str
    last_ts: datetime

    @property
    def despatched_ts(self) -> Optional[datetime]:
        return self.stamps.get("despatched")

    @property
    def despatched_qty(self) -> Optional[int]:
        return self.qty_by_event.get("despatched")


@dataclass
class _CarrierView:
    carrier_id: str
    tracking_no: str
    stamps: dict  # event -> datetime
    last_event: str
    last_ts: datetime

    @property
    def delivered_ts(self) -> Optional[datetime]:
        return self.stamps.get("delivered")


@dataclass
class _InvoiceLine:
    warehouse_id: str
    order_id: str
    line_item: str
    qty: int
    pence: int


@dataclass
class _Sources:
    orders: dict[str, _Order]
    wms: dict[str, _WmsView]
    carrier: dict[str, _CarrierView]
    invoice_lines: list[_InvoiceLine] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _load_sources(data_dir: Path) -> _Sources:
    orders: dict[str, _Order] = {}
    with (data_dir / "platform_orders.csv").open(newline="") as fh:
        for r in csv.DictReader(fh):
            orders[r["order_id"]] = _Order(
                order_id=r["order_id"],
                seller_id=r["seller_id"],
                created_at=parse_ts(r["created_at"]),
                qty=int(r["qty"]),
                status=r["status"].upper(),
                ship_by=parse_ts(r["ship_by_sla"]),
            )

    wms_events: dict[str, list] = defaultdict(list)
    wms_wh: dict[str, str] = {}
    with (data_dir / "wms_events.csv").open(newline="") as fh:
        for r in csv.DictReader(fh):
            wms_events[r["order_id"]].append((r["event"], parse_ts(r["timestamp"]), int(r["qty"])))
            wms_wh[r["order_id"]] = r["warehouse_id"]
    wms: dict[str, _WmsView] = {}
    for oid, events in wms_events.items():
        events.sort(key=lambda e: e[1])
        stamps = {e: ts for e, ts, _ in events}
        qty_by_event = {e: q for e, ts, q in events}
        last_event, last_ts, _ = events[-1]
        wms[oid] = _WmsView(wms_wh[oid], stamps, qty_by_event, last_event, last_ts)

    carrier_events: dict[str, list] = defaultdict(list)
    carrier_meta: dict[str, tuple[str, str]] = {}
    with (data_dir / "carrier_tracking.csv").open(newline="") as fh:
        for r in csv.DictReader(fh):
            carrier_events[r["order_id"]].append((r["event"], parse_ts(r["timestamp"])))
            carrier_meta[r["order_id"]] = (r["carrier_id"], r["tracking_no"])
    carrier: dict[str, _CarrierView] = {}
    for oid, events in carrier_events.items():
        events.sort(key=lambda e: e[1])
        stamps = {e: ts for e, ts in events}
        last_event, last_ts = events[-1]
        cid, trk = carrier_meta[oid]
        carrier[oid] = _CarrierView(cid, trk, stamps, last_event, last_ts)

    invoice_lines: list[_InvoiceLine] = []
    with (data_dir / "invoice.csv").open(newline="") as fh:
        for r in csv.DictReader(fh):
            invoice_lines.append(
                _InvoiceLine(
                    warehouse_id=r["warehouse_id"],
                    order_id=r["order_id"],
                    line_item=r["line_item"],
                    qty=int(r["qty"]),
                    pence=to_pence(r["amount"]),
                )
            )

    return _Sources(orders, wms, carrier, invoice_lines)


# --------------------------------------------------------------------------- #
# Fulfilment rules (Platform × WMS × Carrier)
# --------------------------------------------------------------------------- #
def _fulfilment_exceptions(src: _Sources, as_of: datetime) -> list[ExceptionRecord]:
    out: list[ExceptionRecord] = []

    def flag(order_id: str, t: ExceptionType, party: str, detail: str, evidence: dict,
             recoverable: int = 0) -> None:
        out.append(
            ExceptionRecord(
                order_id=order_id,
                type=t,
                responsible_party=party,
                detail=detail,
                evidence=evidence,
                recoverable_pence=recoverable,
                detected_at=as_of,
            )
        )

    for oid, order in src.orders.items():
        w = src.wms.get(oid)
        home_wh = config.SELLER_HOME_WAREHOUSE.get(order.seller_id, "WMS")

        # --- MISSING_IN_WMS: platform has it, WMS never saw it ------------ #
        if w is None:
            age = hours_between(as_of, order.created_at)
            if age > config.INTAKE_SLA_HOURS and order.status not in {"CANCELLED"}:
                flag(
                    oid,
                    ExceptionType.MISSING_IN_WMS,
                    home_wh,
                    f"No WMS record {age:.0f}h after order creation.",
                    {
                        "created_at": fmt_ts(order.created_at),
                        "hours_since_created": round(age, 1),
                        "status": order.status,
                    },
                )
            continue

        # --- STUCK_NO_MOVEMENT: in WMS but not despatched, gone quiet ----- #
        if w.despatched_ts is None:
            age = hours_between(as_of, w.last_ts)
            if age > config.STUCK_HOURS:
                flag(
                    oid,
                    ExceptionType.STUCK_NO_MOVEMENT,
                    w.warehouse_id,
                    f"Last WMS event '{w.last_event}' {age:.0f}h ago; not despatched.",
                    {
                        "last_event": w.last_event,
                        "last_event_at": fmt_ts(w.last_ts),
                        "hours_stalled": round(age, 1),
                    },
                )
            continue

        # From here the order has a despatch event.
        # --- SLA_DESPATCH_BREACH ----------------------------------------- #
        if w.despatched_ts > order.ship_by:
            late = hours_between(w.despatched_ts, order.ship_by)
            flag(
                oid,
                ExceptionType.SLA_DESPATCH_BREACH,
                w.warehouse_id,
                f"Despatched {late:.0f}h after the ship-by SLA.",
                {
                    "ship_by_sla": fmt_ts(order.ship_by),
                    "despatched_at": fmt_ts(w.despatched_ts),
                    "hours_late": round(late, 1),
                },
            )

        # --- QTY_MISMATCH ------------------------------------------------- #
        if w.despatched_qty is not None and w.despatched_qty != order.qty:
            flag(
                oid,
                ExceptionType.QTY_MISMATCH,
                w.warehouse_id,
                f"Ordered {order.qty}, WMS despatched {w.despatched_qty}.",
                {
                    "order_qty": order.qty,
                    "wms_despatched_qty": w.despatched_qty,
                    "delta": order.qty - w.despatched_qty,
                },
            )

        # --- Carrier-side checks ----------------------------------------- #
        c = src.carrier.get(oid)
        if c is None:
            age = hours_between(as_of, w.despatched_ts)
            if age > config.NOT_SCANNED_HOURS:
                flag(
                    oid,
                    ExceptionType.DESPATCHED_NOT_SCANNED,
                    w.warehouse_id,
                    f"Despatched {age:.0f}h ago with no carrier scan.",
                    {
                        "despatched_at": fmt_ts(w.despatched_ts),
                        "hours_since_despatch": round(age, 1),
                        "carrier_events": 0,
                    },
                )
            continue

        if c.delivered_ts is not None:
            age = hours_between(as_of, c.delivered_ts)
            if age > config.DELIVERED_NOT_CLOSED_HOURS and order.status not in _CLOSED_STATUSES:
                flag(
                    oid,
                    ExceptionType.DELIVERED_NOT_CLOSED,
                    config.PLATFORM_OWNER,
                    f"Delivered {age:.0f}h ago but platform status is '{order.status}'.",
                    {
                        "delivered_at": fmt_ts(c.delivered_ts),
                        "hours_since_delivery": round(age, 1),
                        "platform_status": order.status,
                        "carrier_id": c.carrier_id,
                    },
                )
        else:
            age = hours_between(as_of, c.last_ts)
            if age > config.STALLED_HOURS:
                flag(
                    oid,
                    ExceptionType.TRACKING_STALLED,
                    c.carrier_id,
                    f"Carrier '{c.last_event}' with no update for {age:.0f}h.",
                    {
                        "carrier_id": c.carrier_id,
                        "tracking_no": c.tracking_no,
                        "last_event": c.last_event,
                        "last_event_at": fmt_ts(c.last_ts),
                        "hours_stalled": round(age, 1),
                    },
                )

    return out


# --------------------------------------------------------------------------- #
# Billing rules (Invoice × Rate card × Platform/WMS)
# --------------------------------------------------------------------------- #
def _billing_exceptions(
    src: _Sources, as_of: datetime
) -> tuple[list[ExceptionRecord], InvoiceRecovery]:
    out: list[ExceptionRecord] = []
    total_billed = 0
    total_expected = 0

    by_order: dict[str, list[_InvoiceLine]] = defaultdict(list)
    for line in src.invoice_lines:
        by_order[line.order_id].append(line)

    for order_id, lines in by_order.items():
        wh = lines[0].warehouse_id
        is_phantom_order = order_id not in src.orders

        overbill_delta = 0
        overbill_evidence: list[dict] = []
        dup_amount = 0
        dup_evidence: list[dict] = []
        phantom_amount = 0
        phantom_evidence: list[dict] = []

        occ: dict[str, int] = defaultdict(int)
        for line in lines:
            total_billed += line.pence
            occ[line.line_item] += 1
            rc = config.RATE_CARD.get(line.warehouse_id, {})

            if is_phantom_order:
                phantom_amount += line.pence
                phantom_evidence.append(_line_ev(line, expected=None))
                continue
            if occ[line.line_item] > 1:  # a repeat of an already-billed service
                dup_amount += line.pence
                dup_evidence.append(_line_ev(line, expected=None))
                continue
            if line.line_item not in rc:  # billed for a service off the rate card
                phantom_amount += line.pence
                phantom_evidence.append(_line_ev(line, expected=None))
                continue

            expected = to_pence(rc[line.line_item] * line.qty)
            total_expected += expected
            if line.pence > expected * (1 + config.OVERBILL_TOLERANCE):
                overbill_delta += line.pence - expected
                overbill_evidence.append(_line_ev(line, expected=expected))

        if is_phantom_order:
            out.append(
                ExceptionRecord(
                    order_id=order_id,
                    type=ExceptionType.INVOICE_PHANTOM,
                    responsible_party=wh,
                    detail=f"Invoiced {len(lines)} line(s) with no matching order.",
                    evidence={"lines": phantom_evidence},
                    recoverable_pence=phantom_amount,
                    detected_at=as_of,
                )
            )
            continue

        if overbill_delta > 0:
            out.append(
                ExceptionRecord(
                    order_id=order_id,
                    type=ExceptionType.INVOICE_OVERBILL,
                    responsible_party=wh,
                    detail=f"Billed above the rate card by {_p(overbill_delta)}.",
                    evidence={"lines": overbill_evidence},
                    recoverable_pence=overbill_delta,
                    detected_at=as_of,
                )
            )
        if dup_amount > 0:
            out.append(
                ExceptionRecord(
                    order_id=order_id,
                    type=ExceptionType.INVOICE_DUPLICATE,
                    responsible_party=wh,
                    detail=f"Duplicate billing worth {_p(dup_amount)}.",
                    evidence={"lines": dup_evidence},
                    recoverable_pence=dup_amount,
                    detected_at=as_of,
                )
            )
        if phantom_amount > 0:  # off-rate-card lines on an otherwise real order
            out.append(
                ExceptionRecord(
                    order_id=order_id,
                    type=ExceptionType.INVOICE_PHANTOM,
                    responsible_party=wh,
                    detail=f"Off-rate-card line(s) worth {_p(phantom_amount)}.",
                    evidence={"lines": phantom_evidence},
                    recoverable_pence=phantom_amount,
                    detected_at=as_of,
                )
            )

    return out, InvoiceRecovery(expected_pence=total_expected, billed_pence=total_billed)


def _line_ev(line: _InvoiceLine, expected: Optional[int]) -> dict:
    ev = {
        "warehouse_id": line.warehouse_id,
        "line_item": line.line_item,
        "qty": line.qty,
        "billed": _p(line.pence),
    }
    if expected is not None:
        ev["rate_card_expected"] = _p(expected)
    return ev


def _p(pence: int) -> str:
    return f"£{pence / 100:,.2f}"


# --------------------------------------------------------------------------- #
# SLA attainment
# --------------------------------------------------------------------------- #
def _warehouse_sla(src: _Sources) -> list[WarehouseSLA]:
    despatched: dict[str, int] = defaultdict(int)
    on_time: dict[str, int] = defaultdict(int)
    for oid, w in src.wms.items():
        if w.despatched_ts is None:
            continue
        order = src.orders.get(oid)
        if order is None:
            continue
        despatched[w.warehouse_id] += 1
        if w.despatched_ts <= order.ship_by:
            on_time[w.warehouse_id] += 1
    return [
        WarehouseSLA(wh, despatched.get(wh, 0), on_time.get(wh, 0))
        for wh in config.WAREHOUSES
    ]


def _carrier_sla(src: _Sources, exceptions: list[ExceptionRecord]) -> list[CarrierSLA]:
    shipments: dict[str, int] = defaultdict(int)
    for c in src.carrier.values():
        shipments[c.carrier_id] += 1
    stalled: dict[str, int] = defaultdict(int)
    for e in exceptions:
        if e.type is ExceptionType.TRACKING_STALLED:
            stalled[e.responsible_party] += 1
    return [
        CarrierSLA(cid, shipments.get(cid, 0), stalled.get(cid, 0))
        for cid in config.CARRIERS
    ]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def reconcile(data_dir: Path = config.DATA_DIR, as_of: datetime = config.AS_OF) -> ReconResult:
    """Run the full deterministic reconciliation and return the result."""
    src = _load_sources(data_dir)

    exceptions = _fulfilment_exceptions(src, as_of)
    billing, recovery = _billing_exceptions(src, as_of)
    exceptions.extend(billing)

    result = ReconResult(
        orders_reconciled=len(src.orders),
        exceptions=exceptions,
        warehouse_sla=_warehouse_sla(src),
        carrier_sla=_carrier_sla(src, exceptions),
        invoice_recovery=recovery,
        as_of=as_of,
    )
    return result


def run(data_dir: Path = config.DATA_DIR, as_of: datetime = config.AS_OF) -> ReconResult:
    """Reconcile and persist the result to SQLite."""
    result = reconcile(data_dir, as_of)
    conn = db.connect()
    try:
        db.save_result(conn, result)
    finally:
        conn.close()
    return result
