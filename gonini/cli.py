"""gonini command-line interface.

    gonini seed        generate mock CSVs under /data
    gonini reconcile   run the deterministic engine, persist to SQLite
    gonini digest      produce the morning digest + draft escalations
    gonini demo        seed -> reconcile -> digest, end to end

The LLM is stubbed behind an interface: ``--no-llm`` forces the offline
templates so everything runs without an API key.
"""

from __future__ import annotations

import argparse
from collections import Counter

from . import config, digest, reconcile, seed
from .models import ReconResult
from .util import fmt_gbp


def _print_seed(counts: dict[str, int]) -> None:
    print("Seeded mock data → /data")
    for name, n in counts.items():
        print(f"  {name:<18} {n:>5} rows")


def _print_reconcile(result: ReconResult) -> None:
    print(f"Reconciled {result.orders_reconciled} orders as of {result.as_of}")
    print(f"  {len(result.exceptions)} exception(s) across {result.affected_orders} order(s)")
    by_type = Counter(e.type.value for e in result.exceptions)
    for t, n in sorted(by_type.items()):
        print(f"    {t:<24} {n}")
    print(f"  Recoverable billing: {fmt_gbp(result.invoice_recovery.recoverable_pence)}")
    print(f"  Persisted to {config.DB_PATH}")


def cmd_seed(_: argparse.Namespace) -> int:
    _print_seed(seed.generate())
    return 0


def cmd_reconcile(_: argparse.Namespace) -> int:
    _print_reconcile(reconcile.run())
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    print(digest.run(no_llm=args.no_llm, model=args.model, provider=args.provider))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    print("=" * 72)
    print("gonini demo — seed → reconcile → digest")
    print("=" * 72)
    _print_seed(seed.generate())
    print()
    _print_reconcile(reconcile.run())
    print()
    print("-" * 72)
    print(digest.run(no_llm=args.no_llm, model=args.model, provider=args.provider))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gonini",
        description="Order exception reconciliation agent for a "
        "Fulfilment-as-a-Service platform.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_llm_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--no-llm",
            action="store_true",
            help="use offline template fallbacks instead of a real LLM provider",
        )
        p.add_argument(
            "--provider",
            choices=["auto", "anthropic", "openrouter", "mock"],
            default="auto",
            help="LLM provider (default: auto — OPENROUTER_API_KEY, then "
            "ANTHROPIC_API_KEY, then offline templates)",
        )
        p.add_argument(
            "--model",
            default=None,
            help="model id override (default: "
            f"{config.ANTHROPIC_MODEL} for anthropic, "
            f"{config.OPENROUTER_MODEL} for openrouter)",
        )

    p_seed = sub.add_parser("seed", help="generate mock CSV data under /data")
    p_seed.set_defaults(func=cmd_seed)

    p_rec = sub.add_parser("reconcile", help="run the deterministic engine and persist results")
    p_rec.set_defaults(func=cmd_reconcile)

    p_dig = sub.add_parser("digest", help="produce the morning digest and draft escalations")
    add_llm_flags(p_dig)
    p_dig.set_defaults(func=cmd_digest)

    p_demo = sub.add_parser("demo", help="run seed → reconcile → digest end to end")
    add_llm_flags(p_demo)
    p_demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
