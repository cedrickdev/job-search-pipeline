# tests/v2_builders.py
"""Shared constructors for the V2 domain tests.

Only what more than one test module needs lives here: a `MatchEvaluation` and an
`EligibilityResult`, which `test_v2_decision.py` and `test_v2_policy.py` both
have to hand to something else, and — since Phase 2 — an `Opportunity` and a
`Company`, which the persistence, geography and mapper tests all have to store.
Everything a test is actually asserting on is built inline in that test — a
builder that hides the field under test makes the test unreadable.

The ids are module constants rather than fresh uuid4s for two reasons: a failure
message points at a value one can grep for, and the "belongs to another user"
tests need a second identity that is obviously different.
"""
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from backend.app.domain.common import (
    GeoPoint,
    LanguageLevel,
    LanguageRequirement,
    Location,
    Reason,
    ReasonImpact,
    SalaryPeriod,
    SalaryRange,
    WorkloadRange,
)
from backend.app.domain.company import Company, CompanyLocation
from backend.app.domain.eligibility import (
    DeterminationSource,
    EligibilityCheck,
    EligibilityRequirement,
    EligibilityResult,
    EligibilityStatus,
)
from backend.app.domain.identifiers import (
    ApplicationDecisionId,
    ApplicationPolicyId,
    CandidateProfileId,
    CompanyId,
    CompanyLocationId,
    MatchEvaluationId,
    OpportunityId,
    UserId,
)
from backend.app.domain.matching import DimensionScore, MatchDimension, MatchEvaluation
from backend.app.domain.opportunity import (
    ContractType,
    Opportunity,
    OpportunitySourceRecord,
    OpportunityType,
    WorkplaceMode,
)

NOW = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
LATER = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)

USER = UserId(UUID("00000000-0000-4000-8000-000000000001"))
OTHER_USER = UserId(UUID("00000000-0000-4000-8000-000000000002"))
PROFILE = CandidateProfileId(UUID("00000000-0000-4000-8000-000000000011"))
OTHER_PROFILE = CandidateProfileId(UUID("00000000-0000-4000-8000-000000000012"))
OPPORTUNITY = OpportunityId(UUID("00000000-0000-4000-8000-000000000021"))
OTHER_OPPORTUNITY = OpportunityId(UUID("00000000-0000-4000-8000-000000000022"))
COMPANY = CompanyId(UUID("00000000-0000-4000-8000-000000000031"))
OTHER_COMPANY = CompanyId(UUID("00000000-0000-4000-8000-000000000032"))
COMPANY_LOCATION = CompanyLocationId(UUID("00000000-0000-4000-8000-000000000035"))
EVALUATION = MatchEvaluationId(UUID("00000000-0000-4000-8000-000000000041"))
POLICY = ApplicationPolicyId(UUID("00000000-0000-4000-8000-000000000051"))
DECISION = ApplicationDecisionId(UUID("00000000-0000-4000-8000-000000000061"))

# Somewhere real, so a distance a test asserts on can be checked against a map.
LAUSANNE = GeoPoint(latitude=46.5197, longitude=6.6323)
GENEVA = GeoPoint(latitude=46.2044, longitude=6.1432)      # ~50 km from Lausanne
ZURICH = GeoPoint(latitude=47.3769, longitude=8.5417)      # ~180 km from Lausanne


def a_reason(code="REASON_UNDER_TEST", impact=ReasonImpact.NEUTRAL):
    return Reason(code=code, detail=f"detail behind {code}", impact=impact)


def an_evaluation(**overrides):
    """A high-fit evaluation: one scored dimension, `overall` 0.9."""
    fields = {
        "id": EVALUATION,
        "user_id": USER,
        "candidate_profile_id": PROFILE,
        "opportunity_id": OPPORTUNITY,
        "overall": 0.9,
        "dimensions": (DimensionScore(dimension=MatchDimension.SKILLS_FIT,
                                      score=0.92),),
        "evaluated_at": NOW,
    }
    fields.update(overrides)
    return MatchEvaluation(**fields)


def a_check(requirement=EligibilityRequirement.WORK_AUTHORIZATION,
            status=EligibilityStatus.ELIGIBLE,
            determined_by=DeterminationSource.DETERMINISTIC_RULE):
    """One evaluated gate, with the reason a non-ELIGIBLE verdict must carry."""
    reasons = () if status is EligibilityStatus.ELIGIBLE else (
        a_reason(code=f"{requirement}_{status}", impact=ReasonImpact.NEGATIVE),)
    return EligibilityCheck(requirement=requirement, status=status,
                            determined_by=determined_by, reasons=reasons)


def an_eligibility_result(*checks, **overrides):
    """A result over `checks`, defaulting to a single passing gate."""
    fields = {
        "user_id": USER,
        "candidate_profile_id": PROFILE,
        "opportunity_id": OPPORTUNITY,
        "checks": checks or (a_check(),),
        "determined_at": NOW,
    }
    fields.update(overrides)
    return EligibilityResult(**fields)


def a_source_record(**overrides):
    """Provenance for one posting. `external_id` is what makes it idempotent."""
    fields = {
        "source_key": "test_board",
        "external_id": "posting-1",
        "source_url": "https://example.test/postings/1",
        "fetched_at": NOW,
        "raw": {"title": "Ingenieur logiciel", "employer": "Fixture SA"},
    }
    fields.update(overrides)
    return OpportunitySourceRecord(**fields)


def an_opportunity(**overrides):
    """A posting with every optional column group populated.

    Deliberately full rather than minimal: it is used to prove that a row
    survives a round trip, and a builder that left `salary` or `workload` unset
    would let a mapper forget those columns without failing a test. A test that
    needs the sparse case passes `salary=None`.
    """
    fields = {
        "id": OPPORTUNITY,
        "source": a_source_record(),
        "company_name": "Fixture SA",
        "company_id": None,
        "title": "Ingenieur logiciel",
        "description": "Build and operate the platform.",
        "opportunity_type": OpportunityType.FULL_TIME,
        "contract_type": ContractType.PERMANENT,
        "workplace_mode": WorkplaceMode.HYBRID,
        "workload": WorkloadRange(min_percent=80, max_percent=100),
        "salary": SalaryRange(currency="CHF", period=SalaryPeriod.MONTHLY,
                              minimum=Decimal("4500.10"),
                              maximum=Decimal("6200.00")),
        "location": Location(country="CH", region="Vaud", city="Lausanne",
                             postal_code="1003", point=LAUSANNE,
                             raw="Lausanne, Suisse"),
        "posting_language": "fr",
        "language_requirements": (
            LanguageRequirement(language="fr", minimum_level=LanguageLevel.C1),
            LanguageRequirement(language="en", minimum_level=LanguageLevel.B2,
                                required=False)),
        "posted_at": date(2026, 2, 20),
        "discovered_at": NOW,
        "application_url": "https://example.test/postings/1/apply",
        "dedup_fingerprint": "fingerprint-1",
    }
    fields.update(overrides)
    return Opportunity(**fields)


def a_company_location(**overrides):
    """One site, at Lausanne unless the test says otherwise."""
    fields = {
        "id": COMPANY_LOCATION,
        "company_id": COMPANY,
        "location": Location(country="CH", city="Lausanne", point=LAUSANNE),
        "is_headquarters": True,
    }
    fields.update(overrides)
    return CompanyLocation(**fields)


def a_company(*locations, **overrides):
    """An employer with the sites given, defaulting to a single headquarters."""
    fields = {
        "id": COMPANY,
        "name": "Fixture SA",
        "website": "https://example.test",
        "careers_url": "https://example.test/jobs",
        "locations": locations if locations else (a_company_location(),),
        "accepts_spontaneous_applications": True,
    }
    fields.update(overrides)
    return Company(**fields)
