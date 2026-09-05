# tests/test_v2_base.py
"""`base.py` and `identifiers.py` — the primitives every other V2 model rests on.

The scalar aliases are exercised through a local `DomainModel` subclass rather
than through a real entity, so a failure points at the alias itself and not at
whichever model happened to use it.
"""
import importlib
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.app.domain import identifiers
from backend.app.domain.base import (
    CountryCode,
    CurrencyCode,
    DomainModel,
    HttpUrlStr,
    LanguageCode,
    NonEmptyStr,
    ReasonCode,
    Score,
    UtcDatetime,
)

# Every module of the domain package, in dependency order. The enum sweep at the
# bottom imports all of them, so a module that stops importing fails a test.
DOMAIN_MODULES = (
    "base", "identifiers", "common", "opportunity", "company", "candidate",
    "search", "matching", "eligibility", "policy", "decision",
)


class _Sample(DomainModel):
    """A model that uses every scalar alias exactly once."""

    score: Score | None = None
    when: UtcDatetime | None = None
    text: NonEmptyStr | None = None
    url: HttpUrlStr | None = None
    code: ReasonCode | None = None
    country: CountryCode | None = None
    language: LanguageCode | None = None
    currency: CurrencyCode | None = None
    owner: identifiers.UserId | None = None


def test_score_accepts_the_closed_unit_interval():
    assert _Sample(score=0.0).score == 0.0
    assert _Sample(score=0.5).score == 0.5
    assert _Sample(score=1.0).score == 1.0


def test_score_rejects_anything_outside_the_unit_interval():
    for rejected in (-0.01, 1.01, -1.0, 2.0):
        with pytest.raises(ValidationError):
            _Sample(score=rejected)


def test_score_rejects_the_v1_scale():
    """V1 stores 0-100; feeding one of those numbers in must fail, not rescale.

    `v1_score_to_unit_interval` in the compatibility layer is the only sanctioned
    conversion, and this is what makes forgetting it a loud error.
    """
    with pytest.raises(ValidationError):
        _Sample(score=75)


def test_utc_datetime_refuses_a_naive_instant():
    with pytest.raises(ValidationError) as failure:
        _Sample(when=datetime(2026, 3, 1, 8, 30))
    assert "timezone-aware" in str(failure.value)


def test_utc_datetime_normalizes_an_offset_to_utc():
    aware = datetime(2026, 3, 1, 8, 30, tzinfo=timezone(timedelta(hours=2)))
    stored = _Sample(when=aware).when
    assert stored == datetime(2026, 3, 1, 6, 30, tzinfo=UTC)
    assert stored.tzinfo is UTC


def test_non_empty_str_strips_and_refuses_blank():
    assert _Sample(text="  Migros Vaud  ").text == "Migros Vaud"
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValidationError):
            _Sample(text=blank)


def test_http_url_refuses_every_other_scheme():
    """No adapter downstream may be handed a scheme it should not open."""
    assert _Sample(url="https://jobs.example.test/1").url == "https://jobs.example.test/1"
    assert _Sample(url="http://jobs.example.test/1").url == "http://jobs.example.test/1"
    for rejected in ("mailto:jobs@example.test", "javascript:alert(1)",
                     "file:///etc/passwd", "/jobs/1", ""):
        with pytest.raises(ValidationError):
            _Sample(url=rejected)


def test_reason_code_is_a_screaming_snake_case_identifier():
    for accepted in ("LANGUAGE_BELOW_MINIMUM", "A", "PERMIT_CAP_15H", "X1"):
        assert _Sample(code=accepted).code == accepted
    for rejected in ("lowercase", "1_LEADING_DIGIT", "WITH-HYPHEN", "WITH SPACE",
                     "_LEADING_UNDERSCORE", "", "Mixed_Case"):
        with pytest.raises(ValidationError):
            _Sample(code=rejected)


def test_iso_code_aliases_are_case_and_length_pinned():
    assert _Sample(country="CH").country == "CH"
    assert _Sample(language="fr").language == "fr"
    assert _Sample(currency="CHF").currency == "CHF"
    for field, rejected in (("country", "ch"), ("country", "CHE"), ("country", "C"),
                            ("language", "FR"), ("language", "fra"),
                            ("currency", "chf"), ("currency", "CH")):
        with pytest.raises(ValidationError):
            _Sample(**{field: rejected})


def test_domain_models_are_frozen():
    sample = _Sample(score=0.5)
    with pytest.raises(ValidationError):
        sample.score = 0.6
    assert sample.score == 0.5


def test_domain_models_refuse_an_unknown_field():
    """The LLM-hallucination case: an invented key must fail, not vanish."""
    with pytest.raises(ValidationError) as failure:
        _Sample(score=0.5, invented_by_a_model=True)
    assert "invented_by_a_model" in str(failure.value)


def test_a_change_produces_a_new_value():
    """Frozen means an engine copies rather than mutates."""
    original = _Sample(score=0.5, text="first")
    updated = original.model_copy(update={"score": 0.8})
    assert original.score == 0.5
    assert updated.score == 0.8
    assert updated.text == "first"
    assert original != updated


def test_equal_fields_mean_equal_values():
    assert _Sample(score=0.5, country="CH") == _Sample(score=0.5, country="CH")
    assert _Sample(score=0.5) != _Sample(score=0.6)
    # Hashable while no field holds a mapping — see `OpportunitySourceRecord.raw`
    # in test_v2_opportunity.py for the one documented exception.
    assert hash(_Sample(score=0.5)) == hash(_Sample(score=0.5))


def test_a_newtype_id_field_still_accepts_plain_uuid_input():
    """`NewType` is erased at runtime, so validation must be unaffected."""
    expected = UUID("00000000-0000-4000-8000-0000000000ff")
    assert _Sample(owner=expected).owner == expected
    assert _Sample(owner="00000000-0000-4000-8000-0000000000ff").owner == expected
    with pytest.raises(ValidationError):
        _Sample(owner="not-a-uuid")


def test_every_id_factory_mints_a_distinct_uuid():
    factories = [value for name, value in vars(identifiers).items()
                 if name.startswith("new_") and callable(value)]
    assert len(factories) == 12, "one factory per identifier type"
    minted = [factory() for factory in factories] + [factories[0]()]
    assert all(isinstance(value, UUID) for value in minted)
    assert len(set(minted)) == len(minted)


def test_every_domain_enum_serializes_as_its_own_name():
    """`StrEnum` values are what a database and an API will carry.

    Keeping `value == name` means a stored row is greppable in the source, and a
    renamed member cannot silently keep an old wire value.
    """
    found = []
    for name in DOMAIN_MODULES:
        module = importlib.import_module(f"backend.app.domain.{name}")
        for obj in vars(module).values():
            if isinstance(obj, type) and issubclass(obj, StrEnum) \
                    and obj is not StrEnum and obj.__module__ == module.__name__:
                found.append(obj)
                for member in obj:
                    assert member.value == member.name, f"{obj.__name__}.{member.name}"
    # Guards against a vacuously passing sweep if the filter ever stops matching.
    assert len(found) >= 15
