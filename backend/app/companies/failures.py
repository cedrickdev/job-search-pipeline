"""Provider failures, in Phase 5's vocabulary and through Phase 5's redaction.

Nine lines of real code and a long explanation, because the explanation is the
point: §26 asks that company discovery reuse the existing health semantics and that
no raw exception — with its credentials, tokens, query parameters and headers —
reach a stored or displayed string. The way to guarantee that is to have exactly one
implementation of "read an exception, write a safe sentence", and it already exists
in `backend.app.discovery.failures`.

So this module classifies nothing. It translates a `CompanyProviderMetadata` into
the `SourceMetadata` shape `classify_failure` reads, calls it, and hands back the
result. A second classifier — even a careful one — would be a second place for the
jooble-key leak to reappear.

The translation is lossless in the direction that matters: `credential_env_vars` is
what `redact_secrets` uses to blank *values*, and it is carried across verbatim. No
Phase 6 provider declares one — they read a config file and the local database — but
the field exists on both models precisely so the day one does, redaction covers it
without this module changing.
"""
from datetime import datetime

from backend.app.companies.contracts import (
    CompanyProviderMetadata,
    ProviderFailureCode,
    ProviderHealth,
    ProviderHealthStatus,
)
from backend.app.discovery import failures
from backend.app.discovery.contracts import SourceMetadata, SourceType


def _as_source_metadata(metadata: CompanyProviderMetadata) -> SourceMetadata:
    """The provider, described in the terms `discovery.failures` reads.

    `source_type` is fixed rather than mapped: `classify_failure` never reads it,
    and inventing a correspondence between `CompanyProviderType` and `SourceType`
    would be a mapping nobody uses and everybody has to maintain.
    """
    return SourceMetadata(
        source_key=metadata.provider_key,
        display_name=metadata.display_name,
        source_type=SourceType.COMPANY_CAREER_SITE,
        requires_credentials=metadata.requires_credentials,
        credential_env_vars=metadata.credential_env_vars,
    )


def healthy(metadata: CompanyProviderMetadata, *, checked_at: datetime,
            latency_ms: int | None = None) -> ProviderHealth:
    """The provider answered."""
    return failures.healthy(_as_source_metadata(metadata), checked_at=checked_at,
                            latency_ms=latency_ms)


def degraded(metadata: CompanyProviderMetadata, *, checked_at: datetime,
             detail: str,
             reason: ProviderFailureCode = ProviderFailureCode.SOURCE_PARTIAL_FAILURE,
             latency_ms: int | None = None) -> ProviderHealth:
    """The provider answered partially — some configuration lines, some rows.

    `detail` is caller-written and therefore redacted on the way in, exactly as its
    Phase 5 counterpart is.
    """
    return failures.degraded(_as_source_metadata(metadata), checked_at=checked_at,
                             detail=detail, reason=reason, latency_ms=latency_ms)


def misconfigured(metadata: CompanyProviderMetadata, *, checked_at: datetime,
                  missing: tuple[str, ...] = ()) -> ProviderHealth:
    """Something the provider needs is not set, found before it did any work."""
    return failures.misconfigured(_as_source_metadata(metadata),
                                  checked_at=checked_at, missing=missing)


def health_from_exception(exc: BaseException, metadata: CompanyProviderMetadata, *,
                         checked_at: datetime,
                         latency_ms: int | None = None) -> ProviderHealth:
    """The one call a provider's or the orchestrator's `except` block needs."""
    return failures.health_from_exception(exc, _as_source_metadata(metadata),
                                          checked_at=checked_at,
                                          latency_ms=latency_ms)


# Re-exported so a provider does not have to import from two packages to raise a
# classified failure. `SourceFetchError` is the type `classify_failure` reads a
# `kind` off, and a company provider that wraps a broken configuration file in one
# gets `SOURCE_PARSE_FAILED` instead of the `SOURCE_ADAPTER_ERROR` catch-all.
ProviderFetchError = failures.SourceFetchError

__all__ = [
    "ProviderFailureCode",
    "ProviderFetchError",
    "ProviderHealth",
    "ProviderHealthStatus",
    "degraded",
    "health_from_exception",
    "healthy",
    "misconfigured",
]
