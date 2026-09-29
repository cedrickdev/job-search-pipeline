"""`python -m backend.app.cli.seed_plans` — write the plan catalogue, idempotently (§2, §17, §61).

The explicit operator step that turns an empty `plans` table (a fresh `alembic upgrade head`
creates the table but never its rows) into the `free`/`pro`/`scale` catalogue the entitlement
resolver and the billing surface read. It is **never** run implicitly on API or worker startup —
seeding is a deployment action with its own command, so a running service never races a catalogue
write and an operator decides when pricing changes take effect.

Deployment order: **migrate → seed/reconcile plans → start services.** Re-running is safe: the
seed is idempotent (each tier keeps its `created_at` and reconciles its entitlements), so it
doubles as the reconcile step after a catalogue or pricing change.

Pricing is deployment-configured, never baked in (§17). A paid tier's amount and its provider-side
checkout handle come from the environment:

- `JOBSEARCH_PLAN_PRICE_<SLUG>` — the amount and currency, `"<amount_cents>:<currency>"`
  (e.g. `JOBSEARCH_PLAN_PRICE_PRO=1900:CHF`). A tier with no such variable is seeded *unpriced* —
  coherent, but not sellable until priced — never an invented amount.
- `JOBSEARCH_PLAN_PRICE_ID_<SLUG>` — the opaque provider price handle a checkout needs
  (e.g. `JOBSEARCH_PLAN_PRICE_ID_PRO=price_...`). Absent → no checkout is offered for that tier.

`--demo` seeds the explicitly non-production `DEMO_PLAN_PRICES` instead of reading amounts from the
environment (for fixtures and local runs); it is not for a production catalogue.

Exit codes mirror the other CLIs: 0 clean, 3 finished with a failure (nothing was written —
investigate and re-run; the seed is idempotent).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from os import environ
from typing import Final

from backend.app.billing.catalogue import (
    DEMO_PLAN_PRICES,
    PlanPrice,
    seed_plan_catalogue,
)
from backend.app.core.settings import DatabaseSettings
from backend.app.domain.entitlement import Plan
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.repositories.sqlalchemy_repositories import SqlAlchemyPlanRepository

EXIT_OK: Final[int] = 0
EXIT_INCOMPLETE: Final[int] = 3

_PRICE_ID_PREFIX: Final[str] = "JOBSEARCH_PLAN_PRICE_ID_"
_PRICE_PREFIX: Final[str] = "JOBSEARCH_PLAN_PRICE_"


def plan_pricing_from_env(
        env: Mapping[str, str]) -> tuple[dict[str, str], dict[str, PlanPrice]]:
    """Read per-tier checkout handles and prices from the environment (see the module docstring).

    Pure and deterministic — the CLI passes `os.environ`, a test passes a dict — so the parsing is
    testable without a process environment. Returns `(external_price_ids, prices)`, both keyed by
    the lowercased slug in the variable name. A malformed price raises `ValueError` naming the
    variable (never its value, which is not a secret but need not be echoed either).
    """
    external_price_ids: dict[str, str] = {}
    prices: dict[str, PlanPrice] = {}
    for name, value in env.items():
        if name.startswith(_PRICE_ID_PREFIX):
            slug = name[len(_PRICE_ID_PREFIX):].lower()
            if slug and value:
                external_price_ids[slug] = value
        elif name.startswith(_PRICE_PREFIX):
            slug = name[len(_PRICE_PREFIX):].lower()
            if slug and value:
                prices[slug] = _parse_price(name, value)
    return external_price_ids, prices


def _parse_price(name: str, value: str) -> PlanPrice:
    """`"<amount_cents>:<currency>"` into a `PlanPrice`, or `ValueError` naming the variable."""
    amount_text, _, currency = value.partition(":")
    currency = currency.strip()
    if not currency:
        raise ValueError(f"{name} must be '<amount_cents>:<currency>', e.g. '1900:CHF'")
    try:
        amount_cents = int(amount_text)
    except ValueError:
        raise ValueError(
            f"{name} amount must be a whole number of minor units, e.g. '1900:CHF'") from None
    if amount_cents < 0:
        raise ValueError(f"{name} amount must not be negative")
    return PlanPrice(amount_cents=amount_cents, currency=currency)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli.seed_plans",
        description="Write (or reconcile) the free/pro/scale plan catalogue. Idempotent — run it "
                    "after every migration and after any catalogue or pricing change. Prices and "
                    "checkout handles are read from the environment; --demo uses non-production "
                    "demo pricing instead.",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="seed the non-production DEMO_PLAN_PRICES instead of reading amounts from the "
             "environment (for fixtures and local runs, never a production catalogue)",
    )
    return parser.parse_args(argv)


def format_seed_report(written: Sequence[Plan]) -> str:
    """A data-free tally of what was seeded: each tier's slug, price and whether it is sellable.

    Never a secret — an amount and a currency are product truth, and the checkout handle is
    reported only as present/absent, never its value.
    """
    lines = [f"seeded {len(written)} plan(s):"]
    for plan in sorted(written, key=lambda p: (p.price_amount_cents or 0, p.slug)):
        if plan.price_amount_cents is None:
            price = "unpriced (not sellable)"
        else:
            price = f"{plan.price_amount_cents} {plan.currency}"
        handle = ("checkout handle set" if plan.external_price_id is not None
                  else "no checkout handle")
        lines.append(f"  {plan.slug}: {price}; {handle}")
    return "\n".join(lines)


async def _run_seed(*, demo: bool) -> tuple[Plan, ...]:  # pragma: no cover - integration entrypoint
    external_price_ids, env_prices = plan_pricing_from_env(environ)
    prices: Mapping[str, PlanPrice] = DEMO_PLAN_PRICES if demo else env_prices
    engine = create_async_database_engine(DatabaseSettings.from_env())
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            return await seed_plan_catalogue(
                SqlAlchemyPlanRepository(session), now=datetime.now(UTC),
                external_price_ids=external_price_ids, prices=prices)
    finally:
        await engine.dispose()


def _run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        written = asyncio.run(_run_seed(demo=args.demo))
    except Exception as exc:  # a CLI reports a failure, it does not traceback
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE

    print(format_seed_report(written))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised through _run()
    raise SystemExit(_run())
