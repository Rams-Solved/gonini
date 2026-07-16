"""Central configuration for gonini.

Everything that would, in a real deployment, come from environment variables,
a settings file, or a secrets manager lives here so the demo stays a single
`pip install`-free run. Timestamps are deliberately anchored to a fixed
``AS_OF`` so that seeding, reconciliation, and the digest are reproducible.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

DATA_DIR = PROJECT_ROOT / "data"
OUTBOX_DIR = PROJECT_ROOT / "outbox"
DB_PATH = DATA_DIR / "gonini.db"

PLATFORM_CSV = DATA_DIR / "platform_orders.csv"
WMS_CSV = DATA_DIR / "wms_events.csv"
CARRIER_CSV = DATA_DIR / "carrier_tracking.csv"
RATE_CARD_CSV = DATA_DIR / "rate_card.csv"
INVOICE_CSV = DATA_DIR / "invoice.csv"

# --------------------------------------------------------------------------- #
# Reproducibility
# --------------------------------------------------------------------------- #
SEED = 42

# The reconciliation "now". The daily job runs each morning; everything is
# measured relative to this instant so the demo is deterministic.
AS_OF = datetime(2026, 7, 16, 9, 0, 0)

# Number of orders to generate and the anomaly budget (~15%).
TOTAL_ORDERS = 200

# --------------------------------------------------------------------------- #
# Rule thresholds (hours). These are the knobs an ops team would tune.
# --------------------------------------------------------------------------- #
INTAKE_SLA_HOURS = 24  # a platform order not seen by WMS within this window is late
STUCK_HOURS = 24  # no WMS movement for this long => stuck
NOT_SCANNED_HOURS = 24  # despatched but no carrier scan within this long
STALLED_HOURS = 72  # carrier tracking with no update for this long
DELIVERED_NOT_CLOSED_HOURS = 24  # delivered but platform not closed after this long

# Billing tolerance: bill within 0.1% of the recomputed rate-card figure is fine
# (covers rounding). Anything above is flagged as an overbill.
OVERBILL_TOLERANCE = 0.001

# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
WAREHOUSES = ("WH-LON", "WH-MAN", "WH-BRS")
CARRIERS = ("RMG", "DPD", "EVRI")
CHANNELS = ("shopify", "amazon", "ebay")
SELLERS = tuple(f"SELLER-{i:03d}" for i in range(1, 11))

# Which physical site a seller's stock lives in. Used both to place WMS events
# and to attribute orders that never reached the WMS at all.
SELLER_HOME_WAREHOUSE = {
    seller: WAREHOUSES[i % len(WAREHOUSES)] for i, seller in enumerate(SELLERS)
}

PLATFORM_OWNER = "PLATFORM-OPS"

# Billable fulfilment services and their per-unit list prices (£).
RATE_CARD = {
    "WH-LON": {"pick_pack": 1.35, "packaging": 0.45, "carriage": 4.20, "storage": 0.08},
    "WH-MAN": {"pick_pack": 1.10, "packaging": 0.40, "carriage": 3.95, "storage": 0.06},
    "WH-BRS": {"pick_pack": 1.20, "packaging": 0.42, "carriage": 4.05, "storage": 0.07},
}

# --------------------------------------------------------------------------- #
# LLM layer
# --------------------------------------------------------------------------- #
# The LLM is fenced (see README): it never sees or produces the numbers the
# rules engine computes.
ANTHROPIC_MODEL = "claude-sonnet-5"
# OpenRouter's auto-router: picks a live free model at request time so the
# demo doesn't hardcode a specific free model that might be retired.
OPENROUTER_MODEL = "openrouter/free"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LLM_MAX_TOKENS = 2000
# Retries (with exponential backoff) on 429/5xx before falling back to the
# offline templates. Applies to both real providers.
LLM_RETRY_ATTEMPTS = 3


def ensure_dirs() -> None:
    """Create the data and outbox directories if they do not yet exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
