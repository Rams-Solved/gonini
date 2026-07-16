"""The morning digest and the outbox of drafted escalations.

Structure and every number come from the reconciliation result (loaded from
SQLite). The LLM layer contributes only the headline narrative, a per-exception
root-cause hypothesis, and the escalation email prose. Nothing is ever sent:
emails are written to ``/outbox`` as ``.md`` drafts marked pending human
approval. Signal, not exhaust — the digest is capped at the top ten.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import config, db
from .llm import DigestFacts, LLMClient, get_client
from .models import ExceptionRecord, ReconResult
from .taxonomy import Category
from .util import fmt_gbp, fmt_ts

_DATE = lambda dt: fmt_ts(dt)[:10]  # noqa: E731 - tiny local formatter


# --------------------------------------------------------------------------- #
# Facts assembly (all deterministic)
# --------------------------------------------------------------------------- #
def build_facts(result: ReconResult) -> DigestFacts:
    cats = result.by_category()
    titles = Counter(e.title for e in result.exceptions)
    top_types = [(label, n) for label, n in titles.most_common(3)]

    worst_wh = min(
        (w for w in result.warehouse_sla if w.despatched and w.attainment_pct < 100),
        key=lambda w: w.attainment_pct,
        default=None,
    )
    worst_carrier = min(
        (c for c in result.carrier_sla if c.shipments and c.attainment_pct < 100),
        key=lambda c: c.attainment_pct,
        default=None,
    )

    return DigestFacts(
        as_of=result.as_of,
        orders_reconciled=result.orders_reconciled,
        exception_count=len(result.exceptions),
        affected_orders=result.affected_orders,
        seller_facing=cats[Category.SELLER_FACING],
        money=cats[Category.MONEY],
        hygiene=cats[Category.HYGIENE],
        recoverable=fmt_gbp(result.invoice_recovery.recoverable_pence),
        top_types=top_types,
        worst_warehouse=(
            f"{worst_wh.warehouse_id} ({worst_wh.attainment_pct:.0f}%)" if worst_wh else ""
        ),
        worst_carrier=(
            f"{worst_carrier.carrier_id} ({worst_carrier.attainment_pct:.0f}%)"
            if worst_carrier
            else ""
        ),
    )


# --------------------------------------------------------------------------- #
# Escalation email drafting → /outbox
# --------------------------------------------------------------------------- #
def _party_kind(party: str) -> str:
    if party in config.CARRIERS:
        return "carrier"
    if party in config.WAREHOUSES:
        return "warehouse"
    return "platform"


def _recipient(party: str) -> str:
    kind = _party_kind(party)
    if kind == "carrier":
        return f"{party} Account Management <gonini-support@{party.lower()}.example>"
    if kind == "warehouse":
        return f"{party} Operations <ops+{party.lower()}@fulfilment.example>"
    return "Platform Operations <ops@gonini.example>"


def draft_emails(result: ReconResult, llm: LLMClient) -> list[dict]:
    """Draft one escalation per responsible party. Returns metadata per file."""
    config.ensure_dirs()
    by_party: dict[str, list[ExceptionRecord]] = defaultdict(list)
    for e in result.exceptions:
        by_party[e.responsible_party].append(e)

    drafted: list[dict] = []
    for party in sorted(by_party):
        items = sorted(by_party[party], key=lambda e: e.sort_key)
        kind = _party_kind(party)
        draft = llm.draft_email(party, kind, items, result.as_of)
        recoverable = sum(e.recoverable_pence for e in items)

        filename = f"DRAFT_{party}_{_DATE(result.as_of)}.md".replace(" ", "_")
        path = config.OUTBOX_DIR / filename
        content = "\n".join(
            [
                "> **DRAFT — PENDING HUMAN APPROVAL**  ",
                f"> Drafted by gonini for {party} ({kind}). Not sent — review, edit, "
                "and send manually.",
                "",
                f"**To:** {_recipient(party)}  ",
                f"**Subject:** {draft.subject}",
                "",
                "---",
                "",
                draft.body,
                "",
            ]
        )
        path.write_text(content)
        drafted.append(
            {
                "path": path,
                "party": party,
                "kind": kind,
                "count": len(items),
                "recoverable_pence": recoverable,
            }
        )
    return drafted


# --------------------------------------------------------------------------- #
# Digest markdown
# --------------------------------------------------------------------------- #
def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def render_digest(
    result: ReconResult,
    facts: DigestFacts,
    headline: str,
    top: list[ExceptionRecord],
    root_causes: list[str],
    emails: list[dict],
    mode_note: str,
) -> str:
    lines: list[str] = []
    lines.append("# gonini — Morning Reconciliation Digest")
    lines.append(
        f"_As of {fmt_ts(result.as_of)} · Platform × WMS × Carrier × Invoice · "
        f"narrative by {mode_note}_"
    )
    lines.append("")
    lines.append(f"> {headline}")
    lines.append("")

    # -- headline counts ------------------------------------------------- #
    lines.append("## Headline")
    lines.append(f"- **Orders reconciled:** {result.orders_reconciled}")
    lines.append(
        f"- **Exceptions:** {facts.exception_count} across {facts.affected_orders} order(s) "
        f"— {facts.seller_facing} seller-facing · {facts.money} money · {facts.hygiene} hygiene"
    )
    lines.append(f"- **Recoverable billing:** {facts.recoverable}")
    lines.append(f"- **Escalations drafted:** {len(emails)} (in /outbox, pending approval)")
    lines.append("")

    # -- top 10 ---------------------------------------------------------- #
    lines.append("## Top 10 exceptions")
    lines.append(
        "| # | Order | Type | Sev | Owner | Recommended action | Deadline | £ | "
        "Root-cause (LLM) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, (e, cause) in enumerate(zip(top, root_causes), start=1):
        amount = fmt_gbp(e.recoverable_pence) if e.recoverable_pence else "—"
        lines.append(
            f"| {i} | {e.order_id} | {e.type.value} | {e.severity.label} | "
            f"{e.responsible_party} | {_md_escape(e.action)} | {_DATE(e.deadline())} | "
            f"{amount} | {_md_escape(cause)} |"
        )
    lines.append("")

    # -- SLA ------------------------------------------------------------- #
    lines.append("## SLA attainment")
    lines.append("**Warehouses (despatch SLA)**")
    lines.append("| Warehouse | Despatched | On-time | Attainment |")
    lines.append("|---|---|---|---|")
    for w in result.warehouse_sla:
        lines.append(
            f"| {w.warehouse_id} | {w.despatched} | {w.on_time} | {w.attainment_pct:.1f}% |"
        )
    lines.append("")
    lines.append("**Carriers (delivery health)**")
    lines.append("| Carrier | Shipments | Stalled | Attainment |")
    lines.append("|---|---|---|---|")
    for c in result.carrier_sla:
        lines.append(
            f"| {c.carrier_id} | {c.shipments} | {c.stalled} | {c.attainment_pct:.1f}% |"
        )
    lines.append("")

    # -- invoice recovery ------------------------------------------------ #
    rec = result.invoice_recovery
    lines.append("## Invoice recovery (vs rate card)")
    lines.append("| Metric | Amount |")
    lines.append("|---|---|")
    lines.append(f"| Billed | {fmt_gbp(rec.billed_pence)} |")
    lines.append(f"| Expected (recomputed) | {fmt_gbp(rec.expected_pence)} |")
    lines.append(f"| **Recoverable** | **{fmt_gbp(rec.recoverable_pence)}** |")
    lines.append("")

    # -- escalations ----------------------------------------------------- #
    lines.append("## Drafted escalations")
    lines.append(f"{len(emails)} email(s) written to `/outbox`, marked DRAFT — nothing sent:")
    for m in emails:
        extra = f", {fmt_gbp(m['recoverable_pence'])} recoverable" if m["recoverable_pence"] else ""
        lines.append(
            f"- `{m['path'].name}` → {m['party']} ({m['kind']}): {m['count']} item(s){extra}"
        )
    lines.append("")

    lines.append("---")
    lines.append(
        f"_Signal, not exhaust. Full exception table in SQLite (`{config.DB_PATH.name}`). "
        "All comms are drafts awaiting human approval — nothing was sent._"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run(no_llm: bool, model: str | None = None, provider: str = "auto") -> str:
    """Build the digest and outbox from the persisted reconciliation result.

    Returns the digest markdown (the caller prints it)."""
    conn = db.connect()
    try:
        result = db.load_result(conn)
    finally:
        conn.close()

    llm, mode_note = get_client(no_llm, model, provider)

    # Draft escalations first so the digest can reference them.
    emails = draft_emails(result, llm)

    facts = build_facts(result)
    headline = llm.headline(facts)
    top = result.top(10)
    causes = llm.root_causes(top)

    text = render_digest(result, facts, headline, top, causes, emails, mode_note)

    digest_path = config.OUTBOX_DIR / f"digest_{_DATE(result.as_of)}.md"
    digest_path.write_text(text + "\n")
    return text
