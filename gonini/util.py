"""Small shared helpers: money and timestamp handling.

Money is carried as integer **pence** everywhere inside the engine so that
equality checks and deltas never suffer float drift; it is converted to a
formatted ``£`` string only at the edges (CSV in, digest out).
"""

from __future__ import annotations

from datetime import datetime

TS_FORMAT = "%Y-%m-%dT%H:%M:%S"


def to_pence(pounds: float | str) -> int:
    """Convert a pounds value (e.g. 4.20 or "4.20") to integer pence."""
    return int(round(float(pounds) * 100))


def fmt_gbp(pence: int) -> str:
    """Format integer pence as a human ``£`` string."""
    return f"£{pence / 100:,.2f}"


def parse_ts(value: str) -> datetime:
    """Parse an ISO-ish timestamp written by the seeder."""
    return datetime.fromisoformat(value)


def fmt_ts(dt: datetime) -> str:
    """Serialise a datetime to the canonical CSV/JSON string."""
    return dt.strftime(TS_FORMAT)


def hours_between(later: datetime, earlier: datetime) -> float:
    """Whole-and-fractional hours from ``earlier`` to ``later``."""
    return (later - earlier).total_seconds() / 3600.0
