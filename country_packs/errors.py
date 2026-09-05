"""What goes wrong when a Country Pack is malformed, and how it is reported.

§16 of the phase order is a single sentence — "malformed config must fail loudly
with actionable errors" — and it names the six cases: unknown source key, invalid
ISO country code, unsupported opportunity type, duplicate source key, duplicate
Country Pack, invalid capability name. Each one is a code below, because "loudly"
is not enough on its own: an operator editing `sources.yaml` at 23:00 needs to
know *which* key, in *which* file, and what the accepted values are.

One exception type rather than a hierarchy. Callers do not branch on the class —
they read `code`, which is the stable, greppable, dashboard-groupable part
(docs/ENGINEERING_STANDARDS.md §Observability). A hierarchy would invite `except
DuplicateSourceKey` in a service, which is exactly the coupling a code prevents.
"""
from enum import StrEnum
from pathlib import Path


class CountryPackErrorCode(StrEnum):
    """Every way a pack can be rejected at load time."""

    # The pack directory is missing one of the five files it must have.
    COUNTRY_PACK_FILE_MISSING = "COUNTRY_PACK_FILE_MISSING"
    # The file exists but is not YAML, or is not a mapping at the top level.
    COUNTRY_PACK_MALFORMED_YAML = "COUNTRY_PACK_MALFORMED_YAML"
    # A field failed its typed contract: the message carries pydantic's report.
    COUNTRY_PACK_INVALID_FIELD = "COUNTRY_PACK_INVALID_FIELD"
    # Not ISO-3166-1 alpha-2, or not the country the directory claims.
    COUNTRY_PACK_INVALID_COUNTRY = "COUNTRY_PACK_INVALID_COUNTRY"
    # A `sources.yaml` entry names a source the registry has never heard of. This
    # is the one that catches a typo before a sweep silently skips a board.
    COUNTRY_PACK_UNKNOWN_SOURCE = "COUNTRY_PACK_UNKNOWN_SOURCE"
    COUNTRY_PACK_DUPLICATE_SOURCE = "COUNTRY_PACK_DUPLICATE_SOURCE"
    COUNTRY_PACK_INVALID_CAPABILITY = "COUNTRY_PACK_INVALID_CAPABILITY"
    # A local term maps to something that is not an `OpportunityType` member.
    COUNTRY_PACK_INVALID_OPPORTUNITY_TYPE = "COUNTRY_PACK_INVALID_OPPORTUNITY_TYPE"
    COUNTRY_PACK_INVALID_TERM_MAPPING = "COUNTRY_PACK_INVALID_TERM_MAPPING"
    # Two packs claim the same country, or the same pack was registered twice.
    COUNTRY_PACK_DUPLICATE_PACK = "COUNTRY_PACK_DUPLICATE_PACK"
    # Asked for a country nothing is registered for.
    COUNTRY_PACK_NOT_FOUND = "COUNTRY_PACK_NOT_FOUND"


class CountryPackError(Exception):
    """A pack could not be loaded, registered or found.

    Carries the code, the country it concerns when that is known, and the file it
    came from when that is known. `str(exc)` is built to be pasted into a terminal
    and acted on, not to be parsed.
    """

    def __init__(self, code: CountryPackErrorCode, message: str, *,
                 country: str | None = None, path: Path | None = None) -> None:
        self.code = code
        self.country = country
        self.path = path
        location = f" [{path}]" if path is not None else ""
        subject = f" ({country})" if country is not None else ""
        super().__init__(f"{code}{subject}: {message}{location}")
