"""YAML on disk becomes a validated `CountryPack`, or fails with an address.

§16 is the whole design brief: "malformed config must fail loudly with actionable
errors", and it names six cases. Loudly is the easy half. The hard half is
*actionable*: pydantic's report says `sources.0.expects_capabilities`, which is
correct and useless at 23:00, so every failure below is re-raised as a
`CountryPackError` carrying a `COUNTRY_PACK_*` code, the country, and the file the
operator has to open.

The five files are separate on purpose (see `CountryPack`), and all five are
required. An optional file would mean a pack that loads with an empty terminology
map and classifies nothing — a silent half-configuration, which is the failure
mode §16 exists to prevent.
"""
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import ValidationError

from country_packs.contracts import (
    CountryPack,
    EligibilityMetadata,
    OpportunityTypeMap,
    PackMetadata,
    SourceBinding,
    TerminologyMap,
)
from country_packs.errors import CountryPackError, CountryPackErrorCode

METADATA_FILE: Final = "metadata.yaml"
SOURCES_FILE: Final = "sources.yaml"
OPPORTUNITY_TYPES_FILE: Final = "opportunity_types.yaml"
TERMINOLOGY_FILE: Final = "terminology.yaml"
ELIGIBILITY_FILE: Final = "eligibility.yaml"

REQUIRED_FILES: Final = (METADATA_FILE, SOURCES_FILE, OPPORTUNITY_TYPES_FILE,
                         TERMINOLOGY_FILE, ELIGIBILITY_FILE)


def _read_mapping(path: Path, country: str | None) -> dict[str, Any]:
    """The YAML mapping in `path`, or a `CountryPackError` naming the file."""
    if not path.is_file():
        raise CountryPackError(
            CountryPackErrorCode.COUNTRY_PACK_FILE_MISSING,
            f"a Country Pack needs all of {list(REQUIRED_FILES)}; "
            f"{path.name} is missing",
            country=country, path=path)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # `str(exc)` here is PyYAML's own line/column report on a file the operator
        # wrote. It cannot contain a credential, because a pack file cannot: the
        # only credential-shaped field in the contracts is an `EnvVarName`.
        raise CountryPackError(
            CountryPackErrorCode.COUNTRY_PACK_MALFORMED_YAML,
            f"{path.name} is not valid YAML: {exc}",
            country=country, path=path) from exc
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise CountryPackError(
            CountryPackErrorCode.COUNTRY_PACK_MALFORMED_YAML,
            f"{path.name} must contain a mapping at the top level, not "
            f"{type(payload).__name__}",
            country=country, path=path)
    return payload


def _pick_code(exc: ValidationError,
               default: CountryPackErrorCode) -> CountryPackErrorCode:
    """Turn pydantic's error list into one of §16's named codes.

    Only the cases §16 enumerates get their own code; everything else stays
    `COUNTRY_PACK_INVALID_FIELD`, whose message already carries pydantic's
    field-level detail. Inventing a code per field would produce a vocabulary
    nobody can group by.
    """
    for error in exc.errors():
        location = {str(part) for part in error["loc"]}
        message = error.get("msg", "")
        if "expects_capabilities" in location:
            return CountryPackErrorCode.COUNTRY_PACK_INVALID_CAPABILITY
        if "country" in location:
            return CountryPackErrorCode.COUNTRY_PACK_INVALID_COUNTRY
        if "at most once" in message:
            return CountryPackErrorCode.COUNTRY_PACK_DUPLICATE_SOURCE
    return default


def _build[T](factory: type[T], payload: dict[str, Any], path: Path,
              country: str | None, default: CountryPackErrorCode) -> T:
    try:
        return factory(**payload)
    except ValidationError as exc:
        raise CountryPackError(
            _pick_code(exc, default),
            f"{path.name} failed validation: {exc}",
            country=country, path=path) from exc
    except TypeError as exc:
        # An unexpected top-level key reaches `**payload` as a TypeError rather
        # than a ValidationError; `extra="forbid"` catches the rest.
        raise CountryPackError(
            default, f"{path.name} has an unusable top-level key: {exc}",
            country=country, path=path) from exc


def load_pack(directory: Path | str, *,
              expected_country: str | None = None) -> CountryPack:
    """Read a pack directory into a validated `CountryPack`.

    `expected_country` is the directory's claim about itself: `country_packs/ch`
    passes `"CH"`, and a `metadata.yaml` saying `FR` is rejected rather than
    silently registered under the wrong code. That is the mismatch that would
    otherwise surface as "Swiss searches return nothing", far from its cause.
    """
    root = Path(directory)
    metadata = _build(
        PackMetadata, _read_mapping(root / METADATA_FILE, expected_country),
        root / METADATA_FILE, expected_country,
        CountryPackErrorCode.COUNTRY_PACK_INVALID_FIELD)

    if expected_country is not None and metadata.country != expected_country:
        raise CountryPackError(
            CountryPackErrorCode.COUNTRY_PACK_INVALID_COUNTRY,
            f"the pack directory claims {expected_country} but metadata.yaml "
            f"declares {metadata.country}",
            country=expected_country, path=root / METADATA_FILE)

    country = metadata.country
    sources_payload = _read_mapping(root / SOURCES_FILE, country)
    bindings = tuple(
        _build(SourceBinding, entry, root / SOURCES_FILE, country,
               CountryPackErrorCode.COUNTRY_PACK_INVALID_FIELD)
        for entry in _entries(sources_payload, "sources", root / SOURCES_FILE,
                              country))
    opportunity_types = _build(
        OpportunityTypeMap,
        _read_mapping(root / OPPORTUNITY_TYPES_FILE, country),
        root / OPPORTUNITY_TYPES_FILE, country,
        CountryPackErrorCode.COUNTRY_PACK_INVALID_OPPORTUNITY_TYPE)
    terminology = _build(
        TerminologyMap, _read_mapping(root / TERMINOLOGY_FILE, country),
        root / TERMINOLOGY_FILE, country,
        CountryPackErrorCode.COUNTRY_PACK_INVALID_TERM_MAPPING)
    eligibility = _build(
        EligibilityMetadata, _read_mapping(root / ELIGIBILITY_FILE, country),
        root / ELIGIBILITY_FILE, country,
        CountryPackErrorCode.COUNTRY_PACK_INVALID_FIELD)

    try:
        return CountryPack(metadata=metadata, sources=bindings,
                           opportunity_types=opportunity_types,
                           terminology=terminology, eligibility=eligibility)
    except ValidationError as exc:
        raise CountryPackError(
            _pick_code(exc, CountryPackErrorCode.COUNTRY_PACK_INVALID_FIELD),
            f"the assembled pack is inconsistent: {exc}",
            country=country, path=root) from exc


def _entries(payload: dict[str, Any], key: str, path: Path,
             country: str | None) -> list[dict[str, Any]]:
    """The list of mappings under `key`, or a `CountryPackError`.

    An absent key is an empty list — a country that enables no source is odd but
    legal, and the orchestrator already has to report `NO_SOURCE_SELECTED`. A key
    holding something other than a list of mappings is a mistake, and it fails
    here rather than as an obscure `**` unpacking error twenty frames down.
    """
    raw = payload.get(key, [])
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CountryPackError(
            CountryPackErrorCode.COUNTRY_PACK_MALFORMED_YAML,
            f"{path.name}: {key!r} must be a list, not {type(raw).__name__}",
            country=country, path=path)
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise CountryPackError(
                CountryPackErrorCode.COUNTRY_PACK_MALFORMED_YAML,
                f"{path.name}: {key}[{index}] must be a mapping, not "
                f"{type(entry).__name__}",
                country=country, path=path)
        entries.append(entry)
    return entries



