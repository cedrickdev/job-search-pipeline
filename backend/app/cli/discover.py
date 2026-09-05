"""`python -m backend.app.cli.discover` — one sweep, from the command line.

The V2 counterpart of `python -m pipeline.discover`, and the first caller that
proves the Phase 5 architecture end to end: it builds nothing itself. Composition
comes from `discovery.bootstrap`, the sweep from `DiscoveryOrchestrator`, and this
module owns the arguments, the printed report and the exit status — the same
division `import_v1.py` follows.

Three modes, because two of them answer questions worth asking without touching a
network:

- `--list-sources` — what is registered, what each source claims, and in which
  order Switzerland sweeps them. Offline.
- `--healthcheck` — probe the sources that say a probe is welcome, and report what
  the others already know. The only network mode that fetches nothing on purpose.
- the default — sweep, then print the postings found per source, the health of
  every source asked, and every warning.

Exit status, so a cron job can read one number:

- `0` — the sweep is complete: every source answered fully and nothing was
  truncated or left unfiltered;
- `1` — the sweep could not run: no pack for that country, or a composition error
  such as a pack enabling a source nobody registered;
- `3` — the sweep ran but its answer is not the whole market (a source was
  unavailable, a radius was not honoured, a limit truncated a board). §12 exists
  so this case cannot be mistaken for `0`, and docs/V2_SPECIFICATION.md §22 lists
  hiding failed source health as a non-goal.

Nothing printed here can carry a credential. `SourceHealth.detail` is composed
from a fixed vocabulary by `discovery.failures` and passed through
`redact_secrets` before it exists; the only environment variable this module
prints is a *name*, which `EnvVarName` constrains to `^[A-Z][A-Z0-9_]*$` and which
is the actionable half of a `MISCONFIGURED` report.
"""
import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final

from pydantic import ValidationError

from backend.app.discovery.bootstrap import Discovery, build_discovery
from backend.app.discovery.contracts import SourceHealth, SourceMetadata
from backend.app.discovery.orchestrator import SweepReport
from backend.app.discovery.registry import SourceRegistryError
from backend.app.discovery.requests import DEFAULT_LIMIT, DEFAULT_LOOKBACK_DAYS
from backend.app.domain.common import GeoPoint
from backend.app.domain.identifiers import new_search_profile_id, new_user_id
from backend.app.domain.search import (
    CountrySearchArea,
    RadiusSearchArea,
    RemoteOnlySearchArea,
    SearchArea,
    SearchProfile,
)
from country_packs.errors import CountryPackError

EXIT_OK: Final[int] = 0
EXIT_UNUSABLE: Final[int] = 1
# 2 is argparse's own usage error, so "ran, but the answer is incomplete" skips it.
EXIT_INCOMPLETE: Final[int] = 3


def _point(value: str) -> GeoPoint:
    """`--center 46.5197,6.6323`. Two floats, validated by the domain model.

    A place name is deliberately not accepted: geocoding is Phase 7's, and a CLI
    that guessed coordinates from "Lausanne" would be the first geocoder in the
    codebase and the least visible one.
    """
    try:
        latitude, longitude = (float(part) for part in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected LAT,LON in decimal degrees, got {value!r}") from exc
    return GeoPoint(latitude=latitude, longitude=longitude)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {number}")
    return number


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli.discover",
        description="Sweep one country's enabled sources for one ad-hoc search "
                    "profile. Reads no saved profile and writes nothing: this "
                    "command reports, it does not persist.",
        epilog="A source key given to --source that the country pack does not "
               "enable selects nothing, which is reported as NO_SOURCE_SELECTED "
               "rather than silently swept.")
    parser.add_argument("--country", default="CH", metavar="XX",
                        help="ISO 3166-1 alpha-2 code of the pack to sweep "
                             "(default CH)")
    parser.add_argument("--query", action="append", default=[], metavar="TEXT",
                        help="what to type into a search box; repeatable")
    parser.add_argument("--location", action="append", default=[], metavar="PLACE",
                        help="a place name to search in; repeatable. Sources that "
                             "cannot filter by location say so in a warning")
    parser.add_argument("--source", action="append", default=[], metavar="KEY",
                        help="restrict the sweep to these source keys; repeatable")
    parser.add_argument("--remote-only", action="store_true",
                        help="sweep for remote work rather than for a place")
    parser.add_argument("--center", type=_point, metavar="LAT,LON",
                        help="centre of a radius search, in decimal degrees")
    parser.add_argument("--radius-km", type=float, metavar="KM",
                        help="radius around --center. No current source filters by "
                             "distance; the sweep reports that rather than pretend")
    parser.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT,
                        metavar="N", help=f"cap per source, per request (default "
                                          f"{DEFAULT_LIMIT})")
    parser.add_argument("--lookback-days", type=_positive,
                        default=DEFAULT_LOOKBACK_DAYS, metavar="N",
                        help=f"how far back to ask for (default "
                             f"{DEFAULT_LOOKBACK_DAYS}); only sources claiming "
                             f"INCREMENTAL_DISCOVERY can honour it")
    parser.add_argument("--list-sources", action="store_true",
                        help="print what is registered and exit; makes no request")
    parser.add_argument("--healthcheck", action="store_true",
                        help="probe the sources instead of searching")
    parser.add_argument("--show", type=_positive, default=10, metavar="N",
                        help="how many opportunity titles to print per source "
                             "(default 10); the counts are always complete")
    args = parser.parse_args(argv)
    if (args.center is None) != (args.radius_km is None):
        parser.error("--center and --radius-km are only meaningful together")
    if args.remote_only and args.location:
        parser.error("--remote-only and --location ask for two different searches")
    return args


def _areas(args: argparse.Namespace) -> tuple[SearchArea, ...]:
    """The geographic scope, straight from the flags.

    `--location` becomes a *labelled area* rather than a request field because that
    is where `discovery_requests_for` reads locations from: a profile names places,
    and turning a place into whatever a given board calls a location is the
    mapper's job and then the adapter's.
    """
    areas: list[SearchArea] = []
    if args.remote_only:
        areas.append(RemoteOnlySearchArea(country=args.country))
    else:
        areas.extend(CountrySearchArea(country=args.country, label=location)
                     for location in args.location)
        if not args.location:
            # Nowhere named: sweep the country as a whole, which is what a source
            # handed an empty location returns anyway.
            areas.append(CountrySearchArea(country=args.country))
    if args.center is not None:
        areas.append(RadiusSearchArea(center=args.center,
                                      radius_km=args.radius_km))
    return tuple(areas)


def _profile(args: argparse.Namespace, *, now: datetime) -> SearchProfile:
    """An ad-hoc, unsaved profile — this command's whole input model.

    The ids are minted per invocation and written nowhere. §10 forbids a second
    search-preference model, so "search for this" is expressed as a
    `SearchProfile` even when the profile lives for one process and is discarded.
    """
    return SearchProfile(
        id=new_search_profile_id(),
        user_id=new_user_id(),
        name="command-line sweep",
        areas=_areas(args),
        queries=tuple(args.query),
        source_keys=tuple(args.source),
        created_at=now,
        updated_at=now)


def _source_line(metadata: SourceMetadata, position: int | None = None) -> str:
    """One source, in two lines: who it is, then what it claims it can do.

    The capabilities are printed because they are the answer to almost every
    "why did this board not filter by X" question, and the advisory ones are
    printed separately because a claim that only reranks is not a filter.
    """
    where = ", ".join(metadata.countries) or "any country"
    claims = ", ".join(sorted(metadata.capabilities)) or "nothing"
    advisory = (f"; advisory only: {', '.join(sorted(metadata.advisory_capabilities))}"
                if metadata.advisory_capabilities else "")
    # A *name*, never a value: `EnvVarName` cannot hold a credential by contract.
    needs = (f"; needs {', '.join(metadata.credential_env_vars)}"
             if metadata.requires_credentials else "")
    head = f"  {position:>2}. " if position is not None else "      "
    return (f"{head}{metadata.source_key:<11} {metadata.source_type:<21} {where}\n"
            f"        claims {claims}{advisory}{needs}")


def _format_sources(discovery: Discovery, country: str) -> str:
    """What is registered: this country's sweep order first, then the remainder.

    Both halves matter. The first is what a sweep will do; the second is why a
    board an operator expected is missing — `indeed` serves FR, so no CH pack can
    select it, and that is data rather than a bug.
    """
    pack = discovery.packs.find(country)
    selected = discovery.registry.sources_for(country=country, pack=pack)
    chosen = {source.metadata.source_key for source in selected}
    if pack is None:
        lines = [f"no country pack is registered for {country}; "
                 f"showing what a pack could bind"]
    else:
        lines = [f"{country} sweeps {len(selected)} of "
                 f"{len(discovery.registry)} registered sources, in this order:"]
    lines.extend(_source_line(source.metadata, position)
                 for position, source in enumerate(selected, start=1))
    rest = tuple(source for source in discovery.registry
                 if source.metadata.source_key not in chosen)
    if rest:
        lines.append(f"  not swept for {country} ({len(rest)}): out of country, "
                     f"unbound by the pack, or disabled")
        lines.extend(_source_line(source.metadata) for source in rest)
    return "\n".join(lines)


def _format_health(records: Sequence[SourceHealth]) -> str:
    """One line per source. `detail` is already redacted where it was composed."""
    if not records:
        return "no source answered: nothing is registered for that country"
    lines = ["source health:"]
    for record in records:
        latency = f"{record.latency_ms} ms" if record.latency_ms is not None else "-"
        reason = f"  {record.reason}: {record.detail}" if record.reason else ""
        lines.append(f"  {record.source_key:<11} {record.status:<13} "
                     f"{latency:>8}{reason}")
    return "\n".join(lines)


def _format_report(report: SweepReport, *, show: int) -> str:
    """The sweep, as a human reads it: totals, then per source, then what is missing.

    `complete` is printed near the top and in words. It is the one line that
    separates "nobody is hiring" from "nine boards answered and three were down",
    and burying it under a posting count would defeat the purpose of §12.
    """
    metrics = report.metrics
    lines = [
        f"sweep of {report.country} started {report.started_at.isoformat()}",
        f"  duration        {report.duration_ms} ms",
        f"  sources asked   {len(report.source_keys)}",
        f"  requests made   {metrics.requests_made}",
        f"  postings seen   {metrics.postings_seen}",
        f"  opportunities   {metrics.opportunities_returned}",
        f"  postings skipped{metrics.postings_skipped:>4}",
        f"  complete        {'yes' if report.is_complete else 'no'}",
    ]
    for outcome in report.outcomes:
        result = outcome.result
        lines.append(f"  {outcome.source_key} — {len(result.opportunities)} "
                     f"opportunities from {result.metrics.postings_seen} postings "
                     f"in {result.metrics.requests_made} request(s)")
        for opportunity in result.opportunities[:show]:
            where = opportunity.location.raw if opportunity.location else "—"
            lines.append(f"      {opportunity.title} · {opportunity.company_name} "
                         f"· {where}")
        hidden = len(result.opportunities) - show
        if hidden > 0:
            lines.append(f"      ... and {hidden} more; raise --show to list them")
        lines.extend(f"      warning {warning.code}: {warning.detail}"
                     for warning in result.warnings)
    lines.append(_format_health(report.health))
    if report.warnings:
        lines.append("sweep warnings (about the request, not about one source):")
        lines.extend(f"  {warning.code}: {warning.detail}"
                     for warning in report.warnings)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Build, sweep, print, return a status. Never raises for an expected failure.

    `argv` is a parameter so the test suite can call this as a function instead of
    spawning a process — the same reason `import_v1.main` takes one.
    """
    args = _parse_args(argv)
    try:
        discovery = build_discovery()
    except (CountryPackError, SourceRegistryError) as exc:
        # Composition, not discovery: a malformed pack or a binding no adapter
        # answers. §16 wants this loud, and there is nothing to sweep meanwhile.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE

    if args.list_sources:
        print(_format_sources(discovery, args.country))
        return EXIT_OK

    try:
        if args.healthcheck:
            records = asyncio.run(
                discovery.orchestrator.healthcheck(country=args.country))
            print(_format_health(records))
            return EXIT_OK if all(record.is_usable for record in records) \
                else EXIT_INCOMPLETE

        profile = _profile(args, now=datetime.now(UTC))
        report = asyncio.run(discovery.orchestrator.sweep(
            profile, country=args.country, limit=args.limit,
            lookback_days=args.lookback_days))
    except CountryPackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE
    except ValidationError as exc:
        # A flag the domain refuses: a radius over 500 km, a country code that is
        # not two letters. The model's message names the field.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE

    print(_format_report(report, show=args.show))
    return EXIT_OK if report.is_complete else EXIT_INCOMPLETE


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
