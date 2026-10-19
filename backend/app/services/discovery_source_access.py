"""Fail-closed operator gate for automated Swiss source access."""

from dataclasses import dataclass
from os import environ

from backend.app.discovery.ats_board_catalog import ReviewedAtsCatalog
from backend.app.domain.search import SearchProfile
from country_packs.ch.pack import load as load_ch_pack


class DiscoverySourceAccessDenied(Exception):
    """No reviewed source is available for the requested collection."""


@dataclass(frozen=True)
class SourceAccessPolicy:
    configured_source_keys: tuple[str, ...]
    allowed_source_keys: tuple[str, ...]
    invalid_source_keys: tuple[str, ...] = ()
    reviewed_ats_source_keys: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, catalog: ReviewedAtsCatalog) -> "SourceAccessPolicy":
        configured = load_ch_pack().enabled_source_keys
        raw = environ.get("JOBSEARCH_DISCOVERY_ALLOWED_SOURCES", "")
        allowed = tuple(dict.fromkeys(key.strip().lower() for key in raw.split(",")
                                      if key.strip()))
        unknown = tuple(key for key in allowed if key not in configured)
        return cls(configured_source_keys=configured, allowed_source_keys=allowed,
                   invalid_source_keys=unknown,
                   reviewed_ats_source_keys=tuple(
                       key for key in ("greenhouse", "lever") if catalog.boards[key]))

    def sources_for(self, profile: SearchProfile) -> tuple[str, ...]:
        if self.invalid_source_keys:
            raise DiscoverySourceAccessDenied(
                "Swiss discovery configuration names an unknown or disabled source")
        configured = set(self.configured_source_keys)
        allowed = set(self.allowed_source_keys)
        requested = {key.lower() for key in profile.source_keys}
        if requested and (requested - configured or requested - allowed):
            raise DiscoverySourceAccessDenied(
                "Selected Swiss sources have not been approved for automated collection")
        selected = requested or (configured & allowed)
        if selected & {"greenhouse", "lever"} - set(self.reviewed_ats_source_keys):
            raise DiscoverySourceAccessDenied(
                "Selected Swiss ATS source has no approved reviewed board")
        if not selected:
            raise DiscoverySourceAccessDenied(
                "Swiss discovery is disabled until an operator approves a source")
        return tuple(key for key in self.configured_source_keys if key in selected)
