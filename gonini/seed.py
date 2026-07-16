"""Deterministic mock-data generation.

Produces the five CSVs under ``/data`` that stand in for the daily pulls from
the Platform, WMS, and Carrier systems plus the warehouse rate card and
invoice. Roughly 15% of orders carry a *deliberate* anomaly, one per taxonomy
type, seeded reproducibly.

Timeline discipline: anomalies whose signal is "X happened N hours ago" are
built **backward** from :data:`config.AS_OF` so the age is exact and no event
ever lands in the future; healthy orders that simply need a coherent past are
built forward. An order despatched before it was picked would be embarrassing,
so the ordering invariant received < picked < packed < despatched < label <
collected < in_transit < delivered holds for every generated order.
"""

from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

from . import config
from .util import fmt_ts, to_pence

# Row buffers -------------------------------------------------------------- #
_Platform = list[dict]
_Rows = list[dict]


class _Builder:
    """Accumulates rows for the five CSVs from a single seeded RNG."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.platform: _Rows = []
        self.wms: _Rows = []
        self.carrier: _Rows = []
        self.invoice: _Rows = []
        self._order_seq = 10_000
        self._phantom_seq = 90_000
        self._trk_seq = 5_000_000

    # -- id helpers ------------------------------------------------------- #
    def order_id(self) -> str:
        self._order_seq += 1
        return f"ORD-{self._order_seq}"

    def phantom_id(self) -> str:
        self._phantom_seq += 1
        return f"ORD-{self._phantom_seq}"

    def tracking_no(self) -> str:
        self._trk_seq += 1
        return f"TRK{self._trk_seq:08d}"

    # -- rng conveniences ------------------------------------------------- #
    def gap(self, base: datetime, lo: float, hi: float) -> datetime:
        """A timestamp ``lo``–``hi`` hours after ``base`` (minute resolution)."""
        return base + timedelta(minutes=self.rng.randint(int(lo * 60), int(hi * 60)))

    def seller(self) -> str:
        return self.rng.choice(config.SELLERS)

    def carrier_id(self) -> str:
        return self.rng.choice(config.CARRIERS)

    def sku(self) -> str:
        return f"SKU-{self.rng.randint(1000, 9999)}"

    # -- row emit --------------------------------------------------------- #
    def add_order(
        self,
        order_id: str,
        seller: str,
        created: datetime,
        sku: str,
        qty: int,
        status: str,
        ship_by: datetime,
    ) -> None:
        self.platform.append(
            {
                "order_id": order_id,
                "seller_id": seller,
                "channel": self.rng.choice(config.CHANNELS),
                "created_at": fmt_ts(created),
                "sku": sku,
                "qty": qty,
                "status": status,
                "ship_by_sla": fmt_ts(ship_by),
            }
        )

    def add_wms(self, order_id: str, wh: str, events: list[tuple[str, datetime, int]]) -> None:
        for event, ts, qty in events:
            self.wms.append(
                {
                    "order_id": order_id,
                    "warehouse_id": wh,
                    "event": event,
                    "timestamp": fmt_ts(ts),
                    "qty": qty,
                }
            )

    def add_carrier(
        self, order_id: str, carrier: str, trk: str, events: list[tuple[str, datetime]]
    ) -> None:
        for event, ts in events:
            self.carrier.append(
                {
                    "order_id": order_id,
                    "carrier_id": carrier,
                    "tracking_no": trk,
                    "event": event,
                    "timestamp": fmt_ts(ts),
                }
            )

    def add_invoice(self, wh: str, order_id: str, line_item: str, qty: int, pence: int) -> None:
        self.invoice.append(
            {
                "warehouse_id": wh,
                "order_id": order_id,
                "line_item": line_item,
                "qty": qty,
                "amount": f"{pence / 100:.2f}",
            }
        )

    def bill_standard(self, wh: str, order_id: str, qty: int) -> None:
        """Emit the normal three-line fulfilment invoice priced to the rate card."""
        rc = config.RATE_CARD[wh]
        self.add_invoice(wh, order_id, "pick_pack", qty, to_pence(rc["pick_pack"] * qty))
        self.add_invoice(wh, order_id, "packaging", 1, to_pence(rc["packaging"]))
        self.add_invoice(wh, order_id, "carriage", 1, to_pence(rc["carriage"]))


# --------------------------------------------------------------------------- #
# Lifecycle fragments
# --------------------------------------------------------------------------- #
def _forward_wms(b: _Builder, created: datetime, qty: int) -> tuple[list, datetime]:
    """A complete, on-time received→despatched WMS trail built forward."""
    received = b.gap(created, 1, 6)
    picked = b.gap(received, 1, 8)
    packed = b.gap(picked, 1, 4)
    despatched = b.gap(packed, 1, 4)
    events = [
        ("received", received, qty),
        ("picked", picked, qty),
        ("packed", packed, qty),
        ("despatched", despatched, qty),
    ]
    return events, despatched


def _forward_carrier(b: _Builder, despatched: datetime, to_delivered: bool) -> list:
    """Carrier trail from label to (optionally) delivered, built forward."""
    label = b.gap(despatched, 0.5, 4)
    collected = b.gap(label, 1, 6)
    in_transit = b.gap(collected, 2, 10)
    events = [
        ("label_created", label),
        ("collected", collected),
        ("in_transit", in_transit),
    ]
    if to_delivered:
        events.append(("delivered", b.gap(in_transit, 6, 48)))
    return events


def _backward_wms(b: _Builder, despatched: datetime, qty: int) -> tuple[list, datetime]:
    """A WMS trail ending at a fixed ``despatched`` instant, built backward."""
    packed = despatched - timedelta(minutes=b.rng.randint(60, 180))
    picked = packed - timedelta(minutes=b.rng.randint(60, 240))
    received = picked - timedelta(minutes=b.rng.randint(60, 300))
    created = received - timedelta(minutes=b.rng.randint(60, 300))
    events = [
        ("received", received, qty),
        ("picked", picked, qty),
        ("packed", packed, qty),
        ("despatched", despatched, qty),
    ]
    return events, created


# --------------------------------------------------------------------------- #
# Scenario generators
# --------------------------------------------------------------------------- #
def _clean_delivered(b: _Builder) -> None:
    order_id, seller = b.order_id(), None
    seller = b.seller()
    wh = config.SELLER_HOME_WAREHOUSE[seller]
    qty = b.rng.randint(1, 5)
    created = config.AS_OF - timedelta(
        days=b.rng.randint(5, 12), hours=b.rng.randint(0, 23), minutes=b.rng.randint(0, 59)
    )
    ship_by = created + timedelta(hours=b.rng.choice([48, 72]))
    wms_events, despatched = _forward_wms(b, created, qty)
    carrier_events = _forward_carrier(b, despatched, to_delivered=True)
    b.add_order(order_id, seller, created, b.sku(), qty, "CLOSED", ship_by)
    b.add_wms(order_id, wh, wms_events)
    b.add_carrier(order_id, b.carrier_id(), b.tracking_no(), carrier_events)
    b.bill_standard(wh, order_id, qty)


def _clean_in_progress(b: _Builder) -> None:
    """Healthy, still-in-flight order: despatched, last scan recent (< stall)."""
    order_id = b.order_id()
    seller = b.seller()
    wh = config.SELLER_HOME_WAREHOUSE[seller]
    qty = b.rng.randint(1, 5)
    # Build backward from a recent in_transit scan so nothing lands in the future.
    in_transit = config.AS_OF - timedelta(hours=b.rng.randint(3, 40))
    collected = in_transit - timedelta(minutes=b.rng.randint(120, 480))
    label = collected - timedelta(minutes=b.rng.randint(60, 300))
    despatched = label - timedelta(minutes=b.rng.randint(30, 180))
    wms_events, created = _backward_wms(b, despatched, qty)
    ship_by = created + timedelta(hours=b.rng.choice([48, 72]))
    carrier_events = [
        ("label_created", label),
        ("collected", collected),
        ("in_transit", in_transit),
    ]
    b.add_order(order_id, seller, created, b.sku(), qty, "DESPATCHED", ship_by)
    b.add_wms(order_id, wh, wms_events)
    b.add_carrier(order_id, b.carrier_id(), b.tracking_no(), carrier_events)
    b.bill_standard(wh, order_id, qty)


def _missing_in_wms(b: _Builder) -> None:
    order_id = b.order_id()
    seller = b.seller()
    created = config.AS_OF - timedelta(hours=b.rng.randint(30, 120))
    ship_by = created + timedelta(hours=b.rng.choice([24, 48]))
    b.add_order(order_id, seller, created, b.sku(), b.rng.randint(1, 5), "PROCESSING", ship_by)
    # Intentionally no WMS / carrier / invoice rows.


def _stuck_no_movement(b: _Builder) -> None:
    order_id = b.order_id()
    seller = b.seller()
    wh = config.SELLER_HOME_WAREHOUSE[seller]
    qty = b.rng.randint(1, 5)
    stage = b.rng.choice(["received", "picked", "packed"])
    last_ts = config.AS_OF - timedelta(hours=b.rng.randint(30, 80))
    # Build the trail backward, ending at whichever stage it got stuck on.
    seq = ["received", "picked", "packed"]
    idx = seq.index(stage)
    stamps: dict[str, datetime] = {stage: last_ts}
    cursor = last_ts
    for prev in reversed(seq[:idx]):
        cursor = cursor - timedelta(minutes=b.rng.randint(60, 300))
        stamps[prev] = cursor
    created = stamps[seq[0]] - timedelta(minutes=b.rng.randint(60, 300))
    ship_by = created + timedelta(hours=b.rng.choice([24, 48]))
    events = [(e, stamps[e], qty) for e in seq[: idx + 1]]
    b.add_order(order_id, seller, created, b.sku(), qty, "PROCESSING", ship_by)
    b.add_wms(order_id, wh, events)
    # Not shipped, so not billed.


def _despatched_not_scanned(b: _Builder) -> None:
    order_id = b.order_id()
    seller = b.seller()
    wh = config.SELLER_HOME_WAREHOUSE[seller]
    qty = b.rng.randint(1, 5)
    despatched = config.AS_OF - timedelta(hours=b.rng.randint(30, 70))
    wms_events, created = _backward_wms(b, despatched, qty)
    ship_by = created + timedelta(hours=b.rng.choice([48, 72]))
    b.add_order(order_id, seller, created, b.sku(), qty, "DESPATCHED", ship_by)
    b.add_wms(order_id, wh, wms_events)
    # No carrier rows at all — the parcel never got scanned.
    b.bill_standard(wh, order_id, qty)


def _tracking_stalled(b: _Builder) -> None:
    order_id = b.order_id()
    seller = b.seller()
    wh = config.SELLER_HOME_WAREHOUSE[seller]
    qty = b.rng.randint(1, 5)
    in_transit = config.AS_OF - timedelta(hours=b.rng.randint(80, 140))
    collected = in_transit - timedelta(minutes=b.rng.randint(120, 480))
    label = collected - timedelta(minutes=b.rng.randint(60, 300))
    despatched = label - timedelta(minutes=b.rng.randint(30, 180))
    wms_events, created = _backward_wms(b, despatched, qty)
    ship_by = created + timedelta(hours=b.rng.choice([48, 72]))
    carrier_events = [
        ("label_created", label),
        ("collected", collected),
        ("in_transit", in_transit),
    ]
    b.add_order(order_id, seller, created, b.sku(), qty, "DESPATCHED", ship_by)
    b.add_wms(order_id, wh, wms_events)
    b.add_carrier(order_id, b.carrier_id(), b.tracking_no(), carrier_events)
    b.bill_standard(wh, order_id, qty)


def _qty_mismatch(b: _Builder) -> None:
    order_id = b.order_id()
    seller = b.seller()
    wh = config.SELLER_HOME_WAREHOUSE[seller]
    ordered_qty = b.rng.randint(2, 5)
    shipped_qty = ordered_qty - 1  # short-shipped
    created = config.AS_OF - timedelta(days=b.rng.randint(5, 10), hours=b.rng.randint(0, 23))
    ship_by = created + timedelta(hours=b.rng.choice([48, 72]))
    wms_events, despatched = _forward_wms(b, created, shipped_qty)
    carrier_events = _forward_carrier(b, despatched, to_delivered=True)
    b.add_order(order_id, seller, created, b.sku(), ordered_qty, "CLOSED", ship_by)
    b.add_wms(order_id, wh, wms_events)
    b.add_carrier(order_id, b.carrier_id(), b.tracking_no(), carrier_events)
    # Billed against the order qty, so the invoice itself is not overbilled.
    b.bill_standard(wh, order_id, ordered_qty)


def _delivered_not_closed(b: _Builder) -> None:
    order_id = b.order_id()
    seller = b.seller()
    wh = config.SELLER_HOME_WAREHOUSE[seller]
    qty = b.rng.randint(1, 5)
    delivered = config.AS_OF - timedelta(hours=b.rng.randint(30, 90))
    in_transit = delivered - timedelta(minutes=b.rng.randint(360, 1440))
    collected = in_transit - timedelta(minutes=b.rng.randint(120, 480))
    label = collected - timedelta(minutes=b.rng.randint(60, 300))
    despatched = label - timedelta(minutes=b.rng.randint(30, 180))
    wms_events, created = _backward_wms(b, despatched, qty)
    ship_by = created + timedelta(hours=b.rng.choice([48, 72]))
    carrier_events = [
        ("label_created", label),
        ("collected", collected),
        ("in_transit", in_transit),
        ("delivered", delivered),
    ]
    # Delivered days ago but the platform still shows it in flight.
    b.add_order(order_id, seller, created, b.sku(), qty, "DESPATCHED", ship_by)
    b.add_wms(order_id, wh, wms_events)
    b.add_carrier(order_id, b.carrier_id(), b.tracking_no(), carrier_events)
    b.bill_standard(wh, order_id, qty)


def _sla_despatch_breach(b: _Builder) -> None:
    order_id = b.order_id()
    seller = b.seller()
    wh = config.SELLER_HOME_WAREHOUSE[seller]
    qty = b.rng.randint(1, 5)
    # Aged enough that the late despatch + full carrier trail still lands before AS_OF.
    created = config.AS_OF - timedelta(days=b.rng.randint(7, 11), hours=b.rng.randint(0, 23))
    ship_by = created + timedelta(hours=24)
    despatched = ship_by + timedelta(hours=b.rng.randint(6, 36))  # despatched late
    received = b.gap(created, 1, 5)
    picked = b.gap(received, 1, 6)
    packed = b.gap(picked, 1, 4)
    wms_events = [
        ("received", received, qty),
        ("picked", picked, qty),
        ("packed", packed, qty),
        ("despatched", despatched, qty),
    ]
    carrier_events = _forward_carrier(b, despatched, to_delivered=True)
    b.add_order(order_id, seller, created, b.sku(), qty, "CLOSED", ship_by)
    b.add_wms(order_id, wh, wms_events)
    b.add_carrier(order_id, b.carrier_id(), b.tracking_no(), carrier_events)
    b.bill_standard(wh, order_id, qty)


def _invoice_overbill(b: _Builder) -> None:
    order_id = b.order_id()
    seller = b.seller()
    wh = config.SELLER_HOME_WAREHOUSE[seller]
    qty = b.rng.randint(1, 5)
    created = config.AS_OF - timedelta(days=b.rng.randint(5, 10), hours=b.rng.randint(0, 23))
    ship_by = created + timedelta(hours=b.rng.choice([48, 72]))
    wms_events, despatched = _forward_wms(b, created, qty)
    carrier_events = _forward_carrier(b, despatched, to_delivered=True)
    b.add_order(order_id, seller, created, b.sku(), qty, "CLOSED", ship_by)
    b.add_wms(order_id, wh, wms_events)
    b.add_carrier(order_id, b.carrier_id(), b.tracking_no(), carrier_events)
    rc = config.RATE_CARD[wh]
    inflated = to_pence(rc["pick_pack"] * qty * 1.5)  # ~50% over the rate card
    b.add_invoice(wh, order_id, "pick_pack", qty, inflated)
    b.add_invoice(wh, order_id, "packaging", 1, to_pence(rc["packaging"]))
    b.add_invoice(wh, order_id, "carriage", 1, to_pence(rc["carriage"]))


def _invoice_phantom(b: _Builder) -> None:
    # A billed line for an order id that exists nowhere else.
    order_id = b.phantom_id()
    wh = b.rng.choice(config.WAREHOUSES)
    qty = b.rng.randint(1, 3)
    rc = config.RATE_CARD[wh]
    b.add_invoice(wh, order_id, "pick_pack", qty, to_pence(rc["pick_pack"] * qty))


def _invoice_duplicate(b: _Builder) -> None:
    order_id = b.order_id()
    seller = b.seller()
    wh = config.SELLER_HOME_WAREHOUSE[seller]
    qty = b.rng.randint(1, 5)
    created = config.AS_OF - timedelta(days=b.rng.randint(5, 10), hours=b.rng.randint(0, 23))
    ship_by = created + timedelta(hours=b.rng.choice([48, 72]))
    wms_events, despatched = _forward_wms(b, created, qty)
    carrier_events = _forward_carrier(b, despatched, to_delivered=True)
    b.add_order(order_id, seller, created, b.sku(), qty, "CLOSED", ship_by)
    b.add_wms(order_id, wh, wms_events)
    b.add_carrier(order_id, b.carrier_id(), b.tracking_no(), carrier_events)
    b.bill_standard(wh, order_id, qty)
    # Carriage billed a second time.
    b.add_invoice(wh, order_id, "carriage", 1, to_pence(config.RATE_CARD[wh]["carriage"]))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
#: Each anomaly scenario, generated three times for a countable ~15% rate.
_ANOMALY_SCENARIOS = (
    _missing_in_wms,
    _stuck_no_movement,
    _despatched_not_scanned,
    _tracking_stalled,
    _qty_mismatch,
    _delivered_not_closed,
    _sla_despatch_breach,
    _invoice_overbill,
    _invoice_phantom,
    _invoice_duplicate,
)
_ANOMALY_COPIES = 3
_CLEAN_DELIVERED = 153
_CLEAN_IN_PROGRESS = 20


def _write_csv(path: Path, fieldnames: list[str], rows: _Rows) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def generate(seed: int = config.SEED) -> dict[str, int]:
    """Generate all CSVs and the rate card. Returns row counts per file."""
    config.ensure_dirs()
    b = _Builder(seed)

    # Build the 200 order-bearing scenarios (every anomaly type except the
    # phantom invoice, which never gets an order id) and shuffle the order
    # they run in, so order ids are handed out with anomaly types scattered
    # across the range rather than clustered in a fixed block at the start.
    # The RNG is seeded once, so the mix and its shuffled order are both
    # reproducible.
    order_scenarios = [
        scenario
        for scenario in _ANOMALY_SCENARIOS
        if scenario is not _invoice_phantom
        for _ in range(_ANOMALY_COPIES)
    ]
    order_scenarios += [_clean_delivered] * _CLEAN_DELIVERED
    order_scenarios += [_clean_in_progress] * _CLEAN_IN_PROGRESS
    b.rng.shuffle(order_scenarios)

    for scenario in order_scenarios:
        scenario(b)
    for _ in range(_ANOMALY_COPIES):
        _invoice_phantom(b)

    rate_rows = [
        {"warehouse_id": wh, "service": svc, "unit_price": f"{price:.2f}"}
        for wh, services in config.RATE_CARD.items()
        for svc, price in services.items()
    ]

    _write_csv(
        config.PLATFORM_CSV,
        ["order_id", "seller_id", "channel", "created_at", "sku", "qty", "status", "ship_by_sla"],
        b.platform,
    )
    _write_csv(
        config.WMS_CSV,
        ["order_id", "warehouse_id", "event", "timestamp", "qty"],
        b.wms,
    )
    _write_csv(
        config.CARRIER_CSV,
        ["order_id", "carrier_id", "tracking_no", "event", "timestamp"],
        b.carrier,
    )
    _write_csv(
        config.RATE_CARD_CSV,
        ["warehouse_id", "service", "unit_price"],
        rate_rows,
    )
    _write_csv(
        config.INVOICE_CSV,
        ["warehouse_id", "order_id", "line_item", "qty", "amount"],
        b.invoice,
    )

    return {
        "platform_orders": len(b.platform),
        "wms_events": len(b.wms),
        "carrier_tracking": len(b.carrier),
        "rate_card": len(rate_rows),
        "invoice": len(b.invoice),
    }
