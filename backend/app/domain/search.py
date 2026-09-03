"""`SearchProfile` and `SearchArea` — what a user is looking for, and where.

V1 keeps this in `config/searches.yaml`: a flat list of `queries`, free-text
`locations` ("Yverdon-les-Bains, Suisse"), `title_keywords` and
`exclude_keywords`. It works, but it cannot express "within 25 km of here", it is
single-user, and it filters by blacklisting words — the live config excludes
"stage", "alternance" and "apprenti", which are precisely the opportunity types
docs/V2_SPECIFICATION.md §7 makes first-class. V2 replaces the blacklist with a
typed `opportunity_types` allow-list and free text with `SearchArea`.

`SearchArea` is a discriminated union rather than one model with optional
fields, so an area cannot be malformed: a country area has no radius to leave
unset, and a radius area cannot omit its centre. Nothing here computes
distances — that needs PostGIS and arrives in Phase 7 (docs/ARCHITECTURE.md §11).
"""
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from backend.app.domain.base import (
    CountryCode,
    DomainModel,
    LanguageCode,
    NonEmptyStr,
    UtcDatetime,
)
from backend.app.domain.common import GeoPoint, WorkloadRange
from backend.app.domain.identifiers import SearchProfileId, UserId
from backend.app.domain.opportunity import (
    ContractType,
    OpportunityType,
    WorkplaceMode,
)


class SearchAreaKind(StrEnum):
    """The three geographic shapes a search can take.

    Deliberately only three. A polygon or administrative-region area needs real
    geometry, which the domain does not have until Phase 7 puts PostGIS behind a
    port; declaring the member now would promise a shape nothing can evaluate.
    """

    COUNTRY = "COUNTRY"
    RADIUS = "RADIUS"
    REMOTE_ONLY = "REMOTE_ONLY"


class CountrySearchArea(DomainModel):
    """An entire country."""

    kind: Literal[SearchAreaKind.COUNTRY] = SearchAreaKind.COUNTRY
    country: CountryCode
    label: NonEmptyStr | None = None


class RadiusSearchArea(DomainModel):
    """Everything within `radius_km` of `center`.

    No country field, and that is the point: a 30 km radius around Geneva covers
    two countries, and a search that silently added a country filter would drop
    the French side of a commute the candidate would happily make. Right-to-work
    is an eligibility question (`WorkAuthorization`), not a discovery filter.
    """

    kind: Literal[SearchAreaKind.RADIUS] = SearchAreaKind.RADIUS
    center: GeoPoint
    radius_km: Annotated[float, Field(gt=0.0, le=500.0)]
    label: NonEmptyStr | None = None


class RemoteOnlySearchArea(DomainModel):
    """Location-independent work.

    `country` is an optional narrowing ("remote, but contracted in CH"), not a
    place the work happens.
    """

    kind: Literal[SearchAreaKind.REMOTE_ONLY] = SearchAreaKind.REMOTE_ONLY
    country: CountryCode | None = None
    label: NonEmptyStr | None = None


SearchArea = Annotated[
    CountrySearchArea | RadiusSearchArea | RemoteOnlySearchArea,
    Field(discriminator="kind"),
]


class SearchProfile(DomainModel):
    """One saved search belonging to one user.

    Every collection field follows the same convention, stated once here because
    it is the sort of detail that turns into a filtering bug: **an empty tuple
    means "no restriction", not "match nothing"**. A profile that restricts
    nothing is a broad search, which is a legitimate thing to save.

    `areas` is the exception — at least one is required. An unbounded
    geographic search is not a search, and it is the kind of default that makes a
    discovery run sweep the planet.
    """

    id: SearchProfileId
    user_id: UserId
    name: NonEmptyStr
    is_active: bool = True
    areas: Annotated[tuple[SearchArea, ...], Field(min_length=1)]
    queries: tuple[NonEmptyStr, ...] = ()
    title_keywords: tuple[NonEmptyStr, ...] = ()
    excluded_keywords: tuple[NonEmptyStr, ...] = ()
    opportunity_types: tuple[OpportunityType, ...] = ()
    contract_types: tuple[ContractType, ...] = ()
    workplace_modes: tuple[WorkplaceMode, ...] = ()
    posting_languages: tuple[LanguageCode, ...] = ()
    workload: WorkloadRange | None = None
    source_keys: tuple[NonEmptyStr, ...] = ()
    created_at: UtcDatetime
    updated_at: UtcDatetime
    @model_validator(mode="after")
    def _timestamps_and_scope_are_coherent(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("SearchProfile updated_at must not precede created_at")
        remote_only_areas = all(
            area.kind is SearchAreaKind.REMOTE_ONLY for area in self.areas)
        if remote_only_areas and self.workplace_modes \
                and WorkplaceMode.REMOTE not in self.workplace_modes:
            raise ValueError("a remote-only search cannot exclude REMOTE from "
                             "workplace_modes")
        return self

    @property
    def includes_remote(self) -> bool:
        """Whether this profile wants remote postings at all.

        A discovery service reads it to decide if remote-first sources are worth
        querying; `False` here saves a network sweep, it does not filter results.
        An empty `workplace_modes` restricts nothing and therefore includes
        remote — the same convention `allows_source` follows, and the reason the
        broadest possible search does not quietly skip remote-first boards.
        """
        if any(area.kind is SearchAreaKind.REMOTE_ONLY for area in self.areas):
            return True
        return not self.workplace_modes or WorkplaceMode.REMOTE in self.workplace_modes

    def allows_opportunity_type(self, opportunity_type: OpportunityType | None) -> bool:
        """Apply the allow-list convention to one opportunity type.

        `None` — not yet classified — always passes. Dropping unclassified
        postings here would hide everything a classifier has not reached, which
        looks exactly like a broken source.
        """
        if not self.opportunity_types or opportunity_type is None:
            return True
        return opportunity_type in self.opportunity_types

    def allows_source(self, source_key: str) -> bool:
        """Apply the allow-list convention to one source plugin key."""
        return not self.source_keys or source_key in self.source_keys
