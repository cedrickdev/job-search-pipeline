"""Country packs by ISO code, with a duplicate refused rather than overwritten.

Small on purpose. §8 asks for registry *composition* and discovery
*orchestration* to stay clearly distinct, and the cheapest way to honour that is
for this file to contain no policy at all: it holds packs, hands one back by
country, and refuses to hold two for the same country.

Not a `DomainModel`: a registry is mutable composition state, not a value. It is
built once — `backend.app.discovery.bootstrap` is the only place that builds the
default one — and read many times.
"""
from collections.abc import Iterable

from country_packs.contracts import CountryPack
from country_packs.errors import CountryPackError, CountryPackErrorCode


class CountryPackRegistry:
    """The packs this process knows about.

    `register` refusing a duplicate is §18's "duplicate registration rejected",
    and the reason is that the alternative is worse in a way nobody notices:
    last-write-wins would let an import order decide which Switzerland is in
    effect, and two packs disagreeing about `full_time_weekly_hours` would silently
    change every workload conversion depending on which module was imported first.
    """

    def __init__(self, packs: Iterable[CountryPack] = ()) -> None:
        self._packs: dict[str, CountryPack] = {}
        for pack in packs:
            self.register(pack)

    def register(self, pack: CountryPack) -> None:
        existing = self._packs.get(pack.country)
        if existing is not None:
            raise CountryPackError(
                CountryPackErrorCode.COUNTRY_PACK_DUPLICATE_PACK,
                f"{existing.metadata.display_name} is already registered for this "
                f"country; a country has exactly one pack",
                country=pack.country)
        self._packs[pack.country] = pack

    def get(self, country: str) -> CountryPack:
        """The pack for a country, or `COUNTRY_PACK_NOT_FOUND`.

        Raising rather than returning `None` is deliberate for the *default*
        accessor: a discovery run for an unsupported country must stop with a code
        an operator can act on, not proceed with an empty terminology map and
        report that Switzerland has no jobs.
        """
        pack = self._packs.get(country)
        if pack is None:
            raise CountryPackError(
                CountryPackErrorCode.COUNTRY_PACK_NOT_FOUND,
                f"no Country Pack is registered; known countries: "
                f"{list(self.countries)}",
                country=country)
        return pack

    def find(self, country: str) -> CountryPack | None:
        """The pack for a country, or `None` — for callers that have a fallback."""
        return self._packs.get(country)

    @property
    def countries(self) -> tuple[str, ...]:
        """Registered countries, sorted. Ordering is total, so output is diffable."""
        return tuple(sorted(self._packs))

    @property
    def packs(self) -> tuple[CountryPack, ...]:
        return tuple(self._packs[country] for country in self.countries)

    def __contains__(self, country: object) -> bool:
        return country in self._packs

    def __len__(self) -> int:
        return len(self._packs)
