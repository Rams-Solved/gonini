"""SQLite persistence for reconciliation results.

The store holds only the *outputs* of a run (the exception table and the two
SLA / invoice summaries). The raw system-of-record data stays in the CSVs — in
a real deployment those would be API pulls, and the DB would be the audit log
of what the deterministic engine flagged. Each run replaces the previous one.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config
from .models import (
    CarrierSLA,
    ExceptionRecord,
    InvoiceRecovery,
    ReconResult,
    WarehouseSLA,
)
from .taxonomy import ExceptionType
from .util import parse_ts

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    as_of             TEXT NOT NULL,
    generated_at      TEXT NOT NULL,
    orders_reconciled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS exceptions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          TEXT NOT NULL,
    type              TEXT NOT NULL,
    category          TEXT NOT NULL,
    severity          TEXT NOT NULL,
    responsible_party TEXT NOT NULL,
    recoverable_pence INTEGER NOT NULL,
    detail            TEXT NOT NULL,
    evidence_json     TEXT NOT NULL,
    detected_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS warehouse_sla (
    warehouse_id   TEXT PRIMARY KEY,
    despatched     INTEGER NOT NULL,
    on_time        INTEGER NOT NULL,
    attainment_pct REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS carrier_sla (
    carrier_id     TEXT PRIMARY KEY,
    shipments      INTEGER NOT NULL,
    stalled        INTEGER NOT NULL,
    attainment_pct REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invoice_recovery (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    expected_pence INTEGER NOT NULL,
    billed_pence   INTEGER NOT NULL
);
"""


def connect(path: Path = config.DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the gonini SQLite database."""
    config.ensure_dirs()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _clear(conn: sqlite3.Connection) -> None:
    for table in ("run", "exceptions", "warehouse_sla", "carrier_sla", "invoice_recovery"):
        conn.execute(f"DELETE FROM {table}")


def save_result(conn: sqlite3.Connection, result: ReconResult) -> None:
    """Persist a full reconciliation result, replacing any previous run."""
    init(conn)
    _clear(conn)

    conn.execute(
        "INSERT INTO run (id, as_of, generated_at, orders_reconciled) VALUES (1, ?, ?, ?)",
        (result.as_of.isoformat(), datetime.now().isoformat(timespec="seconds"),
         result.orders_reconciled),
    )
    conn.executemany(
        """INSERT INTO exceptions
           (order_id, type, category, severity, responsible_party,
            recoverable_pence, detail, evidence_json, detected_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                e.order_id,
                e.type.value,
                e.category.name,
                e.severity.name,
                e.responsible_party,
                e.recoverable_pence,
                e.detail,
                e.evidence_json(),
                (e.detected_at or result.as_of).isoformat(),
            )
            for e in result.exceptions
        ],
    )
    conn.executemany(
        "INSERT INTO warehouse_sla VALUES (?, ?, ?, ?)",
        [(w.warehouse_id, w.despatched, w.on_time, w.attainment_pct) for w in result.warehouse_sla],
    )
    conn.executemany(
        "INSERT INTO carrier_sla VALUES (?, ?, ?, ?)",
        [(c.carrier_id, c.shipments, c.stalled, c.attainment_pct) for c in result.carrier_sla],
    )
    conn.execute(
        "INSERT INTO invoice_recovery (id, expected_pence, billed_pence) VALUES (1, ?, ?)",
        (result.invoice_recovery.expected_pence, result.invoice_recovery.billed_pence),
    )
    conn.commit()


def load_result(conn: sqlite3.Connection) -> ReconResult:
    """Reconstruct the latest reconciliation result from the store."""
    run = conn.execute("SELECT * FROM run WHERE id = 1").fetchone()
    if run is None:
        raise RuntimeError("no reconciliation run found — run `gonini reconcile` first")

    exceptions: list[ExceptionRecord] = []
    for row in conn.execute("SELECT * FROM exceptions ORDER BY id"):
        exceptions.append(
            ExceptionRecord(
                order_id=row["order_id"],
                type=ExceptionType(row["type"]),
                responsible_party=row["responsible_party"],
                detail=row["detail"],
                evidence=json.loads(row["evidence_json"]),
                recoverable_pence=row["recoverable_pence"],
                detected_at=parse_ts(row["detected_at"]),
            )
        )

    warehouse_sla = [
        WarehouseSLA(r["warehouse_id"], r["despatched"], r["on_time"])
        for r in conn.execute("SELECT * FROM warehouse_sla ORDER BY warehouse_id")
    ]
    carrier_sla = [
        CarrierSLA(r["carrier_id"], r["shipments"], r["stalled"])
        for r in conn.execute("SELECT * FROM carrier_sla ORDER BY carrier_id")
    ]
    inv = conn.execute("SELECT * FROM invoice_recovery WHERE id = 1").fetchone()
    recovery = InvoiceRecovery(inv["expected_pence"], inv["billed_pence"])

    return ReconResult(
        orders_reconciled=run["orders_reconciled"],
        exceptions=exceptions,
        warehouse_sla=warehouse_sla,
        carrier_sla=carrier_sla,
        invoice_recovery=recovery,
        as_of=parse_ts(run["as_of"]),
    )
