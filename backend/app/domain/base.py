"""Base class and scalar types shared by every V2 domain model.

The domain layer is dependency-light on purpose: it imports the standard library
and Pydantic v2 (the validation library docs/ENGINEERING_STANDARDS.md already
mandates) and nothing else. No SQLite, SQLAlchemy, FastAPI, Playwright or LLM
provider SDK may appear anywhere under `backend/app/domain` — persistence and
providers live behind adapters (docs/ARCHITECTURE.md §1 and §9). A test enforces
that boundary rather than trusting review.
"""
import re
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

_REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ALLOWED_URL_SCHEMES = ("http://", "https://")


class DomainModel(BaseModel):
    """Frozen, closed base model.

    `frozen` makes every domain object a value: an engine that needs a change
    produces a new instance with `model_copy(update=...)`, so instances can be
    handed across layers without defensive copying. The one consequence worth
    knowing is that a model carrying a mapping field (only
    `OpportunitySourceRecord.raw` today) is not hashable; equality, which is what
    tests and deduplication need, works normally.

    `extra="forbid"` turns a misspelled or invented field into a validation
    error instead of a silently dropped value. That matters most for
    LLM-produced payloads (docs/LLM_PROVIDER_ARCHITECTURE.md §8): a model that
    hallucinates a key must fail, not lose it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


def _require_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalize anything else to UTC.

    V1 stores `datetime.now().isoformat()` — local time with no offset — which
    is why its date arithmetic is ambiguous. V2 refuses the ambiguity at the
    type level instead of documenting it (docs/ENGINEERING_STANDARDS.md
    §Database rules: timestamps UTC).
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("timestamp must be timezone-aware (V2 timestamps are UTC)")
    return value.astimezone(UTC)


def _require_text(value: str) -> str:
    """Strip, then insist something is left.

    Order matters: a `min_length` constraint alone accepts "   ", which is how
    blank company names and blank titles get into a database.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("value must not be empty or whitespace-only")
    return stripped


def _require_http_url(value: str) -> str:
    """A URL the platform may fetch or hand to a browser adapter.

    Restricted to http(s) so a source record can never smuggle a `javascript:`
    or `file:` target into a Playwright run downstream.
    """
    if not value.startswith(_ALLOWED_URL_SCHEMES):
        raise ValueError("URL must start with http:// or https://")
    return value


def _require_reason_code(value: str) -> str:
    """Reason and failure codes are stable machine identifiers.

    docs/ENGINEERING_STANDARDS.md §Observability asks for codes such as
    `ELIGIBILITY_INCOMPLETE`. Enforcing the shape keeps them greppable and
    keeps prose out of the field that dashboards group by.
    """
    if not _REASON_CODE_RE.match(value):
        raise ValueError("code must be SCREAMING_SNAKE_CASE, e.g. LANGUAGE_BELOW_MINIMUM")
    return value


# A value on the closed unit interval. Every V2 score, weight and confidence
# uses it; V1's 0-100 integers are converted at the compatibility boundary, so
# no module has to remember which scale it is holding.
Score = Annotated[float, Field(ge=0.0, le=1.0)]

# A timezone-aware instant, normalized to UTC.
UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]

# Text that must carry content: rejects "" and whitespace-only values, and
# stores the stripped form.
NonEmptyStr = Annotated[str, AfterValidator(_require_text)]

# An http(s) URL, kept as a string so round-tripping V1 rows is exact.
HttpUrlStr = Annotated[str, Field(min_length=1), AfterValidator(_require_http_url)]

# A stable machine code for a reason, decision or failure.
ReasonCode = Annotated[str, AfterValidator(_require_reason_code)]

# ISO-3166-1 alpha-2, upper case. Kept as a validated string rather than an enum:
# the platform must not need a code change to search in a new country
# (docs/V2_SPECIFICATION.md §5 puts country knowledge in Country Packs).
CountryCode = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]

# ISO-639-1 alpha-2, lower case, for the same reason.
LanguageCode = Annotated[str, Field(pattern=r"^[a-z]{2}$")]

# ISO-4217 alpha-3, upper case.
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
