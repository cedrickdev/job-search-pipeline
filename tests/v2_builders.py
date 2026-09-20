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

from backend.app.domain.candidate import (
    Availability,
    CandidateEvidence,
    CandidateProfile,
    EvidenceKind,
    EvidenceProvenance,
    WorkAuthorization,
    WorkAuthorizationStatus,
)
from backend.app.domain.documents import (
    CandidateDocument,
    CandidateDocumentType,
    DocumentArtifactRef,
    DocumentGuardReport,
    DocumentStatus,
    DocumentVersion,
    EvidenceBackedText,
    ResumeDocument,
)
from backend.app.domain.common import (
    GeoPoint,
    LanguageLevel,
    LanguageProficiency,
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
    CandidateDocumentId,
    CandidateProfileId,
    CompanyId,
    CompanyLocationId,
    EligibilityResultId,
    EvidenceId,
    MatchEvaluationId,
    OpportunityId,
    SearchProfileId,
    UserId,
    candidate_document_id,
    document_version_id,
)
from backend.app.domain.matching import DimensionScore, MatchDimension, MatchEvaluation
from backend.app.domain.opportunity import (
    ContractType,
    Opportunity,
    OpportunitySourceRecord,
    OpportunityType,
    WorkplaceMode,
)
from backend.app.domain.search import CountrySearchArea, SearchProfile

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
ELIGIBILITY = EligibilityResultId(UUID("00000000-0000-4000-8000-000000000045"))
POLICY = ApplicationPolicyId(UUID("00000000-0000-4000-8000-000000000051"))
DECISION = ApplicationDecisionId(UUID("00000000-0000-4000-8000-000000000061"))
SEARCH_PROFILE = SearchProfileId(UUID("00000000-0000-4000-8000-000000000071"))
OTHER_SEARCH_PROFILE = SearchProfileId(UUID("00000000-0000-4000-8000-000000000072"))
EVIDENCE = EvidenceId(UUID("00000000-0000-4000-8000-000000000081"))
DOCUMENT = CandidateDocumentId(UUID("00000000-0000-4000-8000-000000000091"))

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
        "id": ELIGIBILITY,
        "user_id": USER,
        "candidate_profile_id": PROFILE,
        "opportunity_id": OPPORTUNITY,
        "checks": checks or (a_check(),),
        "determined_at": NOW,
    }
    fields.update(overrides)
    return EligibilityResult(**fields)


def a_candidate_profile(**overrides):
    """A candidate owned by `USER`, based in Lausanne.

    Deliberately sparse where Phase 9 has no data — no evidence, no claims — and
    populated where the engines actually read: a base location, one declared
    language, one work authorization and an availability band. A test that needs
    the empty case passes `languages=()`, `work_authorizations=()` or
    `availability=None`; one that needs a different owner passes `user_id=OTHER_USER`
    with `id=OTHER_PROFILE`.
    """
    fields = {
        "id": PROFILE,
        "user_id": USER,
        "display_name": "Fixture Candidate",
        "base_location": Location(country="CH", region="Vaud", city="Lausanne",
                                  point=LAUSANNE, raw="Lausanne, Suisse"),
        "languages": (LanguageProficiency(language="fr", level=LanguageLevel.C2),),
        "work_authorizations": (
            WorkAuthorization(country="CH",
                              status=WorkAuthorizationStatus.CITIZEN),),
        "availability": Availability(min_weekly_hours=20.0, max_weekly_hours=42.0),
        "updated_at": NOW,
    }
    fields.update(overrides)
    return CandidateProfile(**fields)


def a_work_authorization(**overrides):
    """A single-country right to work, CH/CITIZEN unless overridden."""
    fields = {
        "country": "CH",
        "status": WorkAuthorizationStatus.CITIZEN,
    }
    fields.update(overrides)
    return WorkAuthorization(**fields)


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


def an_evidence_record(**overrides):
    """One `CandidateEvidence` owned by `USER`, a plain CV bullet by default.

    Enough to back a claim or a résumé line: a test that needs a diploma or a
    permit document passes `kind=` and `provenance=`, and one that needs another
    owner passes `user_id=OTHER_USER`.
    """
    fields = {
        "id": EVIDENCE,
        "user_id": USER,
        "kind": EvidenceKind.CV_BULLET,
        "provenance": EvidenceProvenance.MANUAL_USER_INPUT,
        "summary": "Led the checkout rewrite that cut latency by 30%",
        "recorded_at": NOW,
    }
    fields.update(overrides)
    return CandidateEvidence(**fields)


def a_resume_content(*, evidence_id=EVIDENCE, **overrides):
    """Minimal `ResumeDocument` content whose one summary line cites `evidence_id`.

    A résumé with a single evidence-backed summary — enough for a version to be
    valid and to render — leaving the heavier composition to the generator the
    document tests exercise directly.
    """
    fields = {
        "full_name": "Fixture Candidate",
        "headline": "Backend engineer",
        "summary": EvidenceBackedText(
            text="Led the checkout rewrite that cut latency by 30%",
            evidence_ids=(evidence_id,)),
    }
    fields.update(overrides)
    return ResumeDocument(**fields)


def a_rendered_document(*, storage_key, content=None, user_id=USER,
                        candidate_profile_id=PROFILE, opportunity_id=OPPORTUNITY,
                        document_type=CandidateDocumentType.RESUME,
                        id=None, **overrides):
    """A `CandidateDocument` with one RENDERED version pointing at `storage_key`.

    The version carries a passing guard report and an artifact reference, which is
    what a download needs. `storage_key` must be a key the caller has actually
    written bytes under in the artifact store, or the download will raise
    `ArtifactNotFound` — the builder describes the locator, it does not create the
    file. The id defaults to the derived `candidate_document_id`, so a test that
    seeds under a placeholder passes `id=` explicitly.
    """
    resolved_id = id if id is not None else candidate_document_id(
        candidate_profile_id, opportunity_id, document_type.value)
    content = content if content is not None else a_resume_content()
    version = DocumentVersion(
        id=document_version_id(resolved_id, 1),
        version=1,
        status=DocumentStatus.RENDERED,
        language="fr",
        content=content,
        guard_report=DocumentGuardReport(ok=True),
        artifact=DocumentArtifactRef(
            storage_key=storage_key, media_type="application/pdf",
            byte_size=1024, page_count=1, rendered_at=NOW),
        generator_key="deterministic-reference/1",
        created_at=NOW)
    fields = {
        "id": resolved_id,
        "user_id": user_id,
        "candidate_profile_id": candidate_profile_id,
        "opportunity_id": opportunity_id,
        "document_type": document_type,
        "versions": (version,),
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return CandidateDocument(**fields)


def a_search_profile(*areas, **overrides):
    """A saved search owned by `USER`, over the areas given.

    Defaults to a single `CountrySearchArea("CH")` so the derived query is an
    ordinary `EXCLUDE_REMOTE` country search — the shape the saved-search geo
    route exercises. A test that needs a radius or a remote-only search passes its
    own areas; one that needs another owner passes `user_id=OTHER_USER`.
    """
    fields = {
        "id": SEARCH_PROFILE,
        "user_id": USER,
        "name": "Suisse romande",
        "areas": areas if areas else (CountrySearchArea(country="CH"),),
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return SearchProfile(**fields)
