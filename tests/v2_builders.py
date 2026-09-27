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
from backend.app.domain.chat import (
    ChatActionExecution,
    ChatActionExecutionOutcome,
    ChatActionProposal,
    ChatMessage,
    ChatMessageRole,
    Conversation,
    SubmitApplicationAction,
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
from backend.app.domain.interview import (
    DimensionEvaluation,
    EvaluationDimension,
    EvaluationStatus,
    InterviewAnswer,
    InterviewAnswerEvaluation,
    InterviewAnswerFormat,
    InterviewDifficulty,
    InterviewMode,
    InterviewPlan,
    InterviewQuestion,
    InterviewQuestionType,
    InterviewSession,
    InterviewSessionSummary,
    InterviewTopic,
    ReadinessBand,
    ReadinessDimensionSummary,
    SessionReadiness,
    SessionStyle,
)
from backend.app.domain.identifiers import (
    ApplicationDecisionId,
    ApplicationId,
    ApplicationPolicyId,
    CandidateDocumentId,
    CandidateProfileId,
    CareerRecommendationId,
    CompanyId,
    CompanyLocationId,
    ConversationId,
    EligibilityResultId,
    EvidenceId,
    InterviewSessionId,
    LLMConnectionId,
    LLMRunId,
    MatchEvaluationId,
    OpportunityId,
    SearchProfileId,
    StrategyChangeProposalId,
    SubscriptionId,
    SubscriptionEventId,
    UserId,
    application_outcome_id,
    candidate_document_id,
    career_recommendation_evidence_id,
    chat_action_execution_id,
    chat_action_proposal_id,
    chat_message_id,
    document_version_id,
    interview_answer_evaluation_id,
    interview_answer_id,
    interview_question_id,
    interview_session_summary_id,
    plan_id,
    provider_session_id,
    role_classification_id,
    strategy_change_execution_id,
    subscription_id,
    subscription_event_id,
    usage_event_id,
)
from backend.app.domain.decision import (
    ApplicationDecision,
    ApplicationDecisionKind,
)
from backend.app.domain.matching import DimensionScore, MatchDimension, MatchEvaluation
from backend.app.domain.policy import ApplicationPolicy, AutomationMode
from backend.app.domain.opportunity import (
    ContractType,
    Opportunity,
    OpportunitySourceRecord,
    OpportunityType,
    WorkplaceMode,
)
from backend.app.domain.search import CountrySearchArea, SearchProfile
from backend.app.domain.analytics import (
    CAREER_ANALYTICS_VERSION,
    DEFAULT_OBSERVATION_HORIZON_DAYS,
    DimensionKind,
    RateKind,
    TimingKind,
)
from backend.app.domain.outcome import (
    ApplicationOutcome,
    OutcomeKind,
    OutcomeSource,
    OutcomeStatus,
    build_outcome_key,
)
from backend.app.domain.recommendation import (
    CareerRecommendation,
    RecommendationEvidence,
    RecommendationKind,
)
from backend.app.domain.role import (
    RoleClassification,
    RoleFamily,
    RoleFamilyProvenance,
)
from backend.app.domain.strategy_change import (
    SetSearchRadiusChange,
    StrategyChangeExecution,
    StrategyChangeExecutionOutcome,
    StrategyChangeProposal,
    StrategyChangeProposalStatus,
)
from backend.app.domain.entitlement import (
    BillingInterval,
    Entitlement,
    EntitlementKey,
    Plan,
)
from backend.app.domain.subscription import Subscription, SubscriptionStatus
from backend.app.domain.subscription_event import (
    SubscriptionEvent,
    SubscriptionEventOutcome,
)
from backend.app.domain.usage import (
    UsageEvent,
    UsageSourceType,
    build_usage_idempotency_key,
)
from backend.app.billing.provider import (
    NormalizedWebhookEvent,
    ProviderSubscriptionState,
)
from backend.app.llm.connection import LLMConnection, LLMProviderType
from backend.app.llm.contracts import TaskPurpose
from backend.app.llm.sessions import ProviderSession
from backend.app.llm.telemetry import LLMRun, LLMRunStatus

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
CONNECTION = LLMConnectionId(UUID("00000000-0000-4000-8000-0000000000a1"))
OTHER_CONNECTION = LLMConnectionId(UUID("00000000-0000-4000-8000-0000000000a2"))
RUN = LLMRunId(UUID("00000000-0000-4000-8000-0000000000b1"))
CONVERSATION = ConversationId(UUID("00000000-0000-4000-8000-0000000000c1"))
OTHER_CONVERSATION = ConversationId(UUID("00000000-0000-4000-8000-0000000000c2"))
APPLICATION = ApplicationId(UUID("00000000-0000-4000-8000-0000000000d1"))
OTHER_APPLICATION = ApplicationId(UUID("00000000-0000-4000-8000-0000000000d2"))
# An interview session's id is random in production (`new_interview_session_id`), so the
# builders pin a constant instead of calling the factory — the question, answer, evaluation
# and summary ids all derive from it, so a whole session's rows stay reproducible.
SESSION = InterviewSessionId(UUID("00000000-0000-4000-8000-0000000000e1"))
OTHER_SESSION = InterviewSessionId(UUID("00000000-0000-4000-8000-0000000000e2"))
# A recommendation's id is random in production (`new_career_recommendation_id`) and a
# proposal's likewise (`new_strategy_change_proposal_id`), so the builders pin a constant
# for each — the evidence ids derive from the recommendation and the execution id from the
# proposal, so a whole recommendation's or proposal's rows stay reproducible across a run.
RECOMMENDATION = CareerRecommendationId(UUID("00000000-0000-4000-8000-0000000000f1"))
OTHER_RECOMMENDATION = CareerRecommendationId(UUID("00000000-0000-4000-8000-0000000000f2"))
STRATEGY_PROPOSAL = StrategyChangeProposalId(UUID("00000000-0000-4000-8000-000000000101"))
OTHER_STRATEGY_PROPOSAL = StrategyChangeProposalId(
    UUID("00000000-0000-4000-8000-000000000102"))
# A plan's id is derived from its stable slug (`plan_id`) and a subscription's from the
# provider's own subscription handle (`subscription_id`), so the builders derive both from
# their natural keys rather than pinning a bare UUID — a re-seed or a redelivered webhook
# lands on the same id the service would compute, which is the whole point of the derivation.
FREE_PLAN = plan_id("free")
PRO_PLAN = plan_id("pro")
OTHER_PLAN = plan_id("scale")
SUBSCRIPTION = subscription_id("stripe", "sub_test_0001")
OTHER_SUBSCRIPTION = subscription_id("stripe", "sub_test_0002")
# A processed billing event's id derives from the provider's own event id
# (`subscription_event_id`), so a redelivery computes the same id the service would — the
# ledger's idempotency in one value. Two event handles for the redelivery/out-of-order tests.
SUBSCRIPTION_EVENT = subscription_event_id("stripe", "evt_test_0001")
OTHER_SUBSCRIPTION_EVENT = subscription_event_id("stripe", "evt_test_0002")

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


def a_policy(**overrides):
    """An application policy owned by `USER`.

    Defaults to the cautious `MANUAL` policy onboarding would create — brake on, no
    autonomy — so a test that wants an autonomous one opts in explicitly with
    `mode=AutomationMode.AUTOPILOT, require_approval_before_submission=False`.
    """
    fields = {
        "id": POLICY,
        "user_id": USER,
        "name": "Default policy",
        "mode": AutomationMode.MANUAL,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return ApplicationPolicy(**fields)


def an_autopilot_policy(**overrides):
    """A policy that permits unattended submission: AUTOPILOT with the brake off."""
    fields = {
        "mode": AutomationMode.AUTOPILOT,
        "require_approval_before_submission": False,
    }
    fields.update(overrides)
    return a_policy(**fields)


def a_decision(**overrides):
    """An AUTO_APPLY decision for the fixture pair, with a positive reason.

    Carries no `match`/`eligibility` by default so a test can supply exactly the
    verdicts it is exercising; a submitting kind with an INELIGIBLE eligibility would
    be refused by the domain, which is the point of keeping them separate here.
    """
    fields = {
        "id": DECISION,
        "user_id": USER,
        "candidate_profile_id": PROFILE,
        "opportunity_id": OPPORTUNITY,
        "policy_id": POLICY,
        "kind": ApplicationDecisionKind.AUTO_APPLY,
        "reasons": (a_reason(code="MEETS_POLICY", impact=ReasonImpact.POSITIVE),),
        "decided_at": NOW,
    }
    fields.update(overrides)
    return ApplicationDecision(**fields)


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


def an_llm_connection(**overrides):
    """A user's OpenAI-compatible connection, with a stored (placeholder) credential.

    Defaults to the shape that carries the most columns — a remote API connection
    with a base URL, a model and an encrypted key — so a mapper that dropped one
    fails a round-trip test. `encrypted_api_key` is an opaque placeholder ciphertext,
    never a real key; a test that wants a CLI connection passes
    `provider_type=LLMProviderType.CLAUDE_CODE, base_url=None, encrypted_api_key=None,
    secret_version=None`, and one that wants another owner passes `user_id=OTHER_USER`.
    """
    fields = {
        "id": CONNECTION,
        "user_id": USER,
        "provider_type": LLMProviderType.OPENAI_COMPATIBLE,
        "display_name": "Work gateway",
        "base_url": "https://gateway.example.invalid/v1",
        "model": "external-model",
        "encrypted_api_key": "gAAAAAB-placeholder-ciphertext-not-a-real-key",
        "secret_version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return LLMConnection(**fields)


def a_provider_session(*, connection_id=CONNECTION, conversation_key="chat-1",
                       **overrides):
    """A provider session for one conversation on one connection.

    The id derives from `(connection_id, conversation_key)` — the same rule the
    domain enforces — so overriding either through the keyword parameters keeps the
    id it would actually be stored under.
    """
    fields = {
        "id": provider_session_id(connection_id, conversation_key),
        "user_id": USER,
        "connection_id": connection_id,
        "conversation_key": conversation_key,
        "purpose": TaskPurpose.CAREER_CHAT,
        "external_session_id": "provider-session-abc",
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return ProviderSession(**fields)


def an_llm_run(**overrides):
    """A succeeded telemetry run with every measured field populated.

    Deliberately full — tokens, cost, latency, a connection and a model — so a
    round-trip proves the mapper carried each. A test that needs the unknown-is-null
    case passes `prompt_tokens=None` (and so on); one that needs a failure passes
    `status=LLMRunStatus.FAILED, failure_code=...`.
    """
    fields = {
        "id": RUN,
        "user_id": USER,
        "connection_id": CONNECTION,
        "provider_key": "openai_compatible",
        "provider_type": LLMProviderType.OPENAI_COMPATIBLE,
        "model": "external-model",
        "purpose": TaskPurpose.RESUME_TAILORING,
        "status": LLMRunStatus.SUCCEEDED,
        "prompt_tokens": 1200,
        "completion_tokens": 300,
        "total_tokens": 1500,
        "cost_usd": 0.012,
        "latency_ms": 840,
        "started_at": NOW,
        "finished_at": LATER,
    }
    fields.update(overrides)
    return LLMRun(**fields)


def a_conversation(**overrides):
    """A career-chat thread owned by `USER`, with one turn's worth of activity.

    Defaults to an active (non-archived) thread whose `last_message_at` is set, so a
    round trip proves every column carried. A test that needs the just-created empty
    thread passes `last_message_at=None`; one that needs another owner passes
    `user_id=OTHER_USER` with `id=OTHER_CONVERSATION`.
    """
    fields = {
        "id": CONVERSATION,
        "user_id": USER,
        "title": "Postuler chez Fixture SA",
        "created_at": NOW,
        "updated_at": NOW,
        "last_message_at": LATER,
    }
    fields.update(overrides)
    return Conversation(**fields)


def a_chat_message(*, conversation_id=CONVERSATION, sequence=0,
                   role=ChatMessageRole.USER, **overrides):
    """One turn in a conversation, a user message at sequence 0 by default.

    The id derives from `(conversation_id, sequence)` — the rule the service applies — so
    re-finalizing the same turn writes the same row rather than duplicating it. A user
    message carries no LLM provenance; an assistant one opts in with
    `role=ChatMessageRole.ASSISTANT, llm_run_id=RUN, provider_key="openai_compatible"`,
    which is the only shape the `user_has_no_run` CHECK permits to hold telemetry.
    """
    fields = {
        "id": chat_message_id(conversation_id, sequence),
        "conversation_id": conversation_id,
        "user_id": USER,
        "role": role,
        "content": "Peux-tu preparer ma candidature ?",
        "sequence": sequence,
        "created_at": NOW,
    }
    fields.update(overrides)
    return ChatMessage(**fields)


def a_chat_action_proposal(*, conversation_id=CONVERSATION, message_id=None,
                           sequence=1, ordinal=0, action=None, **overrides):
    """One typed action proposed in a turn, `PROPOSED` and awaiting confirmation.

    Defaults to a `SubmitApplicationAction` — the heaviest-weight action, so a round trip
    proves the JSONB payload and the denormalized `kind` column agree. The id derives from
    `(message_id, ordinal)`; `message_id` defaults to the assistant turn at `sequence`
    (1 by default, since the assistant answers the user's opening turn).
    """
    resolved_message_id = (message_id if message_id is not None
                           else chat_message_id(conversation_id, sequence))
    resolved_action = (action if action is not None
                       else SubmitApplicationAction(application_id=APPLICATION))
    fields = {
        "id": chat_action_proposal_id(resolved_message_id, ordinal),
        "conversation_id": conversation_id,
        "message_id": resolved_message_id,
        "user_id": USER,
        "ordinal": ordinal,
        "action": resolved_action,
        "summary": "Soumettre la candidature",
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return ChatActionProposal(**fields)


def a_chat_action_execution(*, proposal_id=None, **overrides):
    """The audit of one confirmed proposal, a `SUCCEEDED` outcome by default.

    The id derives from the proposal, so a double-confirm collapses onto one row rather
    than running the action twice. `proposal_id` defaults to the derived id of the default
    proposal above; a test that seeds a different proposal passes its id explicitly.
    """
    resolved_proposal_id = (
        proposal_id if proposal_id is not None
        else chat_action_proposal_id(chat_message_id(CONVERSATION, 1), 0))
    fields = {
        "id": chat_action_execution_id(resolved_proposal_id),
        "proposal_id": resolved_proposal_id,
        "user_id": USER,
        "outcome": ChatActionExecutionOutcome.SUCCEEDED,
        "detail": "Candidature soumise",
        "result_ref": "SUBMITTED",
        "created_at": NOW,
    }
    fields.update(overrides)
    return ChatActionExecution(**fields)


def an_interview_plan(**overrides):
    """A two-topic behavioural plan, the yardstick a session's coverage is measured against.

    Defaults to `InterviewMode.BEHAVIORAL` with two distinct topics, so `plan.mode` agrees
    with `an_interview_session`'s default mode. A test for another mode passes both `mode`
    and `topics`; the plan's own validator refuses a repeated topic label.
    """
    fields = {
        "mode": InterviewMode.BEHAVIORAL,
        "topics": (
            InterviewTopic(label="past teamwork",
                           question_type=InterviewQuestionType.BEHAVIORAL,
                           target_questions=2),
            InterviewTopic(label="motivation",
                           question_type=InterviewQuestionType.MOTIVATION),
        ),
    }
    fields.update(overrides)
    return InterviewPlan(**fields)


def an_interview_session(**overrides):
    """A freshly created behavioural session owned by `USER`, rehearsing `OPPORTUNITY`.

    Defaults to `CREATED` with no `ended_at`, the state the lifecycle invariant requires of
    a non-terminal session. A test that needs a finished session passes
    `status=InterviewSessionStatus.COMPLETED, ended_at=LATER`; one for another owner passes
    `user_id=OTHER_USER` with `id=OTHER_SESSION`. Override `mode` together with `plan`, since
    the two must agree.
    """
    fields = {
        "id": SESSION,
        "user_id": USER,
        "candidate_profile_id": PROFILE,
        "opportunity_id": OPPORTUNITY,
        "mode": InterviewMode.BEHAVIORAL,
        "style": SessionStyle.COACHING,
        "difficulty": InterviewDifficulty.INTERMEDIATE,
        "plan": an_interview_plan(),
        "title": "Entretien comportemental — Fixture SA",
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return InterviewSession(**fields)


def an_interview_question(*, session_id=SESSION, sequence=0,
                          question_type=InterviewQuestionType.BEHAVIORAL, **overrides):
    """One primary question at sequence 0 by default, its id derived from the session.

    The id is `(session_id, sequence)`, the rule the engine applies, so re-finalizing the
    same turn writes the same row. A primary question follows nothing (`depth=0`,
    `follows_sequence=None`); an adaptive follow-up passes `depth=1, follows_sequence=0` at a
    later `sequence`, the only shape the `follow_up_shape_coherent` invariant permits.
    """
    fields = {
        "id": interview_question_id(session_id, sequence),
        "session_id": session_id,
        "user_id": USER,
        "sequence": sequence,
        "question_type": question_type,
        "difficulty": InterviewDifficulty.INTERMEDIATE,
        "prompt": "Parlez-moi d'une fois où vous avez résolu un conflit d'équipe.",
        "topic_label": "past teamwork",
        "asked_at": NOW,
    }
    fields.update(overrides)
    return InterviewQuestion(**fields)


def an_interview_answer(*, question_id=None, session_id=SESSION, **overrides):
    """The candidate's one TEXT answer to a question, its id derived from that question.

    `question_id` defaults to the id of the default primary question above; the answer id
    derives from it, so a resubmit lands on the same row. A voice answer passes
    `format=InterviewAnswerFormat.VOICE, transcript_confidence=0.9` — a TEXT answer must not
    carry a `transcript_confidence`, which the invariant enforces.
    """
    resolved_question_id = (question_id if question_id is not None
                            else interview_question_id(session_id, 0))
    fields = {
        "id": interview_answer_id(resolved_question_id),
        "question_id": resolved_question_id,
        "session_id": session_id,
        "user_id": USER,
        "format": InterviewAnswerFormat.TEXT,
        "content": "J'ai médiatisé un désaccord sur le choix d'une base de données.",
        "answered_at": LATER,
    }
    fields.update(overrides)
    return InterviewAnswer(**fields)


def a_dimension_evaluation(dimension=EvaluationDimension.CLARITY,
                           status=EvaluationStatus.EVALUATED, score=0.8, **overrides):
    """One axis of an answer's grade, `EVALUATED` at 0.8 by default.

    An `EVALUATED` dimension must carry a score and a `NOT_EVALUATED` one must not — the
    `NOT_EVALUATED` ≠ zero rule — so an honest "not assessed" axis passes
    `status=EvaluationStatus.NOT_EVALUATED, score=None`.
    """
    fields = {"dimension": dimension, "status": status, "score": score}
    fields.update(overrides)
    return DimensionEvaluation(**fields)


def an_answer_evaluation(*, answer_id=None, session_id=SESSION, dimensions=None,
                         **overrides):
    """The structured grade of one answer — two evaluated axes, and no readiness.

    `answer_id` defaults to the id of the default answer above; the evaluation id derives
    from it, so a re-grade overwrites the one row. The default `dimensions` are two distinct
    evaluated axes; the model forbids a repeated dimension and — pointedly — has no readiness
    field, so a round trip proves the grade persists without one.
    """
    resolved_answer_id = (
        answer_id if answer_id is not None
        else interview_answer_id(interview_question_id(session_id, 0)))
    fields = {
        "id": interview_answer_evaluation_id(resolved_answer_id),
        "answer_id": resolved_answer_id,
        "session_id": session_id,
        "user_id": USER,
        "dimensions": dimensions if dimensions is not None else (
            a_dimension_evaluation(dimension=EvaluationDimension.CLARITY, score=0.8),
            a_dimension_evaluation(dimension=EvaluationDimension.RELEVANCE, score=0.7),
        ),
        "confidence": 0.6,
        "strengths": ("structure claire de la réponse",),
        "improvements": ("chiffrer davantage l'impact",),
        "evaluated_at": LATER,
    }
    fields.update(overrides)
    return InterviewAnswerEvaluation(**fields)


def a_session_readiness(**overrides):
    """A `PROGRESSING` readiness computed at 0.75 over one evaluated axis.

    A coaching signal, never a probability: `overall` and `band` move together
    (`overall=None` ⟺ `band=UNKNOWN`), so an unassessable session builds with
    `overall=None, band=ReadinessBand.UNKNOWN, dimensions=(<count 0, mean None>,)`. `coverage`
    is the orthogonal "how much of the plan was exercised" axis, reported beside the score.
    """
    fields = {
        "overall": 0.75,
        "band": ReadinessBand.PROGRESSING,
        "dimensions": (
            ReadinessDimensionSummary(
                dimension=EvaluationDimension.CLARITY, mean_score=0.8,
                evaluated_count=1, weight=0.3),
        ),
        "coverage": 0.5,
        "answered_questions": 1,
        "evaluated_answers": 1,
        "profile_version": "interview-readiness/1.0",
        "computed_at": LATER,
    }
    fields.update(overrides)
    return SessionReadiness(**fields)


def an_interview_session_summary(*, session_id=SESSION, readiness=None, **overrides):
    """The one closing summary per session — deterministic readiness plus guarded prose.

    The id derives from the session, so completing a session twice reuses the row. It pairs
    a `SessionReadiness` (defaulting to `a_session_readiness()`) with a `headline` about the
    *practice* — never a hiring forecast — and the at-a-glance counts feed the history.
    """
    fields = {
        "id": interview_session_summary_id(session_id),
        "session_id": session_id,
        "user_id": USER,
        "readiness": readiness if readiness is not None else a_session_readiness(),
        "headline": "Motivation claire, exemples encore trop généraux.",
        "strengths": ("articule clairement ses motivations",),
        "focus_areas": ("étayer chaque réussite d'un résultat chiffré",),
        "questions_asked": 1,
        "answers_evaluated": 1,
        "created_at": LATER,
    }
    fields.update(overrides)
    return InterviewSessionSummary(**fields)


# --- Phase 15: outcomes, classifications, recommendations and strategy changes ---------


def an_application_outcome(*, application_id=APPLICATION, kind=OutcomeKind.INTERVIEW,
                           occurred_at=NOW, supersedes_id=None, **overrides):
    """One recorded real-world hiring milestone of an application, owned by `USER`.

    The `outcome_key` and the id both derive from `(kind, occurred_at, supersedes_id)` via
    `build_outcome_key`, exactly as the service composes them, so recording the same milestone
    twice collapses onto the one row. Defaults to an effective, manually-recorded `INTERVIEW`
    — a repeatable milestone whose key folds in `occurred_at` — with `recorded_at` kept
    strictly apart from `occurred_at` (§8). A correction passes `supersedes_id=` (its key then
    keys on the predecessor); a retraction or supersede passes `status=`. Nothing here touches
    a Phase 12 `ApplicationState` (§2, §84).
    """
    outcome_key = build_outcome_key(kind=kind, occurred_at=occurred_at,
                                    supersedes_id=supersedes_id)
    fields = {
        "id": application_outcome_id(application_id, outcome_key),
        "user_id": USER,
        "application_id": application_id,
        "kind": kind,
        "source": OutcomeSource.MANUAL_USER,
        "status": OutcomeStatus.EFFECTIVE,
        "outcome_key": outcome_key,
        "occurred_at": occurred_at,
        "recorded_at": LATER,
        "supersedes_id": supersedes_id,
        "detail": "Premier entretien avec l'équipe.",
    }
    fields.update(overrides)
    return ApplicationOutcome(**fields)


def a_role_classification(*, user_id=USER, opportunity_id=OPPORTUNITY, **overrides):
    """One user's role-family verdict on an opportunity, keyed on `(user_id, opportunity_id)`.

    The id derives from that pair, so re-classifying updates the one row. Defaults to a
    deterministic `SOFTWARE_ENGINEERING` classification. A manual correction passes
    `provenance=RoleFamilyProvenance.MANUAL` with a named `role_family`; the honest
    unclassified gap passes `role_family=None` (only a deterministic classification may leave
    it unset). A different owner passes `user_id=OTHER_USER`, which the id follows.
    """
    fields = {
        "id": role_classification_id(user_id, opportunity_id),
        "user_id": user_id,
        "opportunity_id": opportunity_id,
        "role_family": RoleFamily.SOFTWARE_ENGINEERING,
        "provenance": RoleFamilyProvenance.DETERMINISTIC_TITLE,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return RoleClassification(**fields)


def a_recommendation_evidence(*, recommendation_id=RECOMMENDATION, ordinal=0, **overrides):
    """One computed metric backing a recommendation, its id derived from `(recommendation_id,
    ordinal)`.

    Defaults to a rate citation — a `RESPONSE` rate of 6/20 in the `DATA_AND_ANALYTICS` role
    family, a sample of 20 comfortably above `MIN_RECOMMENDATION_SAMPLE_SIZE`. The one-metric
    rule means a timing citation passes `rate_kind=None, timing_kind=..., numerator=None,
    denominator=None, median_days=...`; an overall (whole-funnel) metric passes
    `dimension=None, dimension_key=None`.
    """
    fields = {
        "id": career_recommendation_evidence_id(recommendation_id, ordinal),
        "recommendation_id": recommendation_id,
        "ordinal": ordinal,
        "dimension": DimensionKind.ROLE_FAMILY,
        "dimension_key": RoleFamily.DATA_AND_ANALYTICS.value,
        "rate_kind": RateKind.RESPONSE,
        "timing_kind": None,
        "numerator": 6,
        "denominator": 20,
        "median_days": None,
        "sample_size": 20,
        "detail": "Data & Analytics : 6 réponses sur 20 candidatures.",
    }
    fields.update(overrides)
    return RecommendationEvidence(**fields)


def a_career_recommendation(*, id=RECOMMENDATION, evidence=None, **overrides):
    """An evidence-backed suggestion owned by `USER`, with one strong citation.

    Each evidence item's `recommendation_id` must equal the recommendation's id — the model
    enforces it — so `evidence` defaults to a single `a_recommendation_evidence` bound to `id`.
    Defaults to a `PRIORITIZE_ROLE_FAMILY` suggestion drawn under the current analytics
    version and written deterministically (no `generator_key`, no `llm_run_id`); a
    model-worded one passes `generator_key=` and `llm_run_id=RUN`. The analytics snapshot it
    pins defaults to `NOW`, the default horizon and an unset (both-`None`) window; a test probing
    the window passes `window_start=`/`window_end=`.
    """
    resolved_evidence = (evidence if evidence is not None
                         else (a_recommendation_evidence(recommendation_id=id),))
    fields = {
        "id": id,
        "user_id": USER,
        "kind": RecommendationKind.PRIORITIZE_ROLE_FAMILY,
        "analytics_version": CAREER_ANALYTICS_VERSION,
        "analytics_computed_at": NOW,
        "observation_horizon_days": DEFAULT_OBSERVATION_HORIZON_DAYS,
        "window_start": None,
        "window_end": None,
        "summary": "Vos candidatures Data & Analytics obtiennent plus de réponses.",
        "detail": None,
        "evidence": resolved_evidence,
        "generator_key": None,
        "llm_run_id": None,
        "created_at": NOW,
    }
    fields.update(overrides)
    return CareerRecommendation(**fields)


def a_strategy_change_proposal(*, id=STRATEGY_PROPOSAL, change=None, **overrides):
    """A proposed edit to one search or policy, `PROPOSED` and awaiting confirmation.

    Defaults to a `SetSearchRadiusChange` — a non-sensitive discovery-scope edit — and derives
    `target`/`target_id` from it, so the queryable columns agree with the payload (the model
    refuses a disagreement). `target_version` is the target's `updated_at` the executor
    revalidates against on approval, and `expires_at` follows `created_at`. A sensitive policy
    edit passes its own `change=` (e.g. a `SetMinimumScoreChange` that lowers the floor); the
    linked recommendation passes `source_recommendation_id=RECOMMENDATION`.
    """
    resolved_change = (change if change is not None else SetSearchRadiusChange(
        search_profile_id=SEARCH_PROFILE, radius_km=40.0, before_radius_km=25.0))
    fields = {
        "id": id,
        "user_id": USER,
        "target": resolved_change.target,
        "target_id": resolved_change.target_ref,
        "change": resolved_change,
        "target_version": NOW,
        "summary": "Élargir le rayon de recherche à 40 km.",
        "source_recommendation_id": None,
        "generator_key": None,
        "llm_run_id": None,
        "status": StrategyChangeProposalStatus.PROPOSED,
        "created_at": NOW,
        "updated_at": NOW,
        "expires_at": datetime(2026, 3, 8, 9, 30, tzinfo=UTC),
    }
    fields.update(overrides)
    return StrategyChangeProposal(**fields)


def a_strategy_change_execution(*, proposal_id=STRATEGY_PROPOSAL, **overrides):
    """The audit of one attempt to apply a confirmed proposal, `SUCCEEDED` by default.

    The id derives from the proposal alone, so a double-confirm collapses onto the one row
    rather than applying the change twice. A refused-at-revalidation attempt passes
    `outcome=StrategyChangeExecutionOutcome.REJECTED` (a stale version, a vanished target); a
    permitted-but-failed service call passes `FAILED`.
    """
    fields = {
        "id": strategy_change_execution_id(proposal_id),
        "proposal_id": proposal_id,
        "user_id": USER,
        "outcome": StrategyChangeExecutionOutcome.SUCCEEDED,
        "observed_target_version": NOW,
        "detail": "Rayon de recherche porté à 40 km.",
        "result_ref": "SEARCH_PROFILE_UPDATED",
        "created_at": LATER,
    }
    fields.update(overrides)
    return StrategyChangeExecution(**fields)


def an_entitlement(*, key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=5, **overrides):
    """One capability ceiling on a plan. Defaults to a finite 5 submissions; `limit=None` is
    unlimited."""
    fields = {"key": key, "limit": limit}
    fields.update(overrides)
    return Entitlement(**fields)


def a_plan(*, slug="pro", entitlements=None, **overrides):
    """A server-authoritative plan owned by no one, its id derived from `slug`.

    Defaults to a paid monthly `pro` plan granting a finite submission and token allowance; a
    free plan passes `slug="free"`, `price_amount_cents=None`, `currency=None`,
    `billing_interval=None` and its own smaller `entitlements`. The `external_price_id` is the
    opaque handle the billing adapter maps to a provider price — a fixture value here, never a
    real Stripe id.
    """
    resolved = (entitlements if entitlements is not None else (
        an_entitlement(key=EntitlementKey.APPLICATION_SUBMISSIONS, limit=100),
        an_entitlement(key=EntitlementKey.LLM_TOKENS, limit=1_000_000),
        an_entitlement(key=EntitlementKey.ACTIVE_SEARCH_PROFILES, limit=10),
    ))
    fields = {
        "id": plan_id(slug),
        "slug": slug,
        "name": f"Plan {slug.title()}",
        "description": None,
        "price_amount_cents": 1900,
        "currency": "CHF",
        "billing_interval": BillingInterval.MONTHLY,
        "external_price_id": f"price_fixture_{slug}",
        "entitlements": resolved,
        "is_public": True,
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return Plan(**fields)


def a_subscription(*, id=SUBSCRIPTION, plan=PRO_PLAN, **overrides):
    """One account's `ACTIVE` subscription to a plan, backed by the `stripe` provider.

    Defaults to an active subscription owned by `USER` with a bounded paid window `[NOW, +30d)`
    and the provider handles a webhook would carry. A trialing/past-due/canceled state passes
    `status=`; the internal free tier passes `provider="internal"`, `external_*=None` and an
    unset window. `provider_event_at` seeds the out-of-order guard.
    """
    fields = {
        "id": id,
        "user_id": USER,
        "plan_id": plan,
        "status": SubscriptionStatus.ACTIVE,
        "provider": "stripe",
        "external_customer_id": "cus_fixture_0001",
        "external_subscription_id": "sub_test_0001",
        "current_period_start": NOW,
        "current_period_end": datetime(2026, 3, 31, 9, 30, tzinfo=UTC),
        "cancel_at_period_end": False,
        "provider_event_at": NOW,
        "provider_event_sequence": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return Subscription(**fields)


def a_usage_event(*, entitlement_key=EntitlementKey.APPLICATION_SUBMISSIONS,
                  source_type=UsageSourceType.APPLICATION_SUBMISSION,
                  source_id=None, quantity=1, **overrides):
    """One measured consumption owned by `USER`, its id derived from its idempotency key.

    Defaults to a single application submission in the `2026-03` period. The idempotency key and
    the id both derive from `(entitlement_key, source_type, source_id)`, so re-metering the same
    source collapses onto one row — the model refuses an id or key that does not match the fact.
    `source_id` defaults to the fixture `APPLICATION`'s id; an LLM-token event passes
    `entitlement_key=EntitlementKey.LLM_TOKENS, source_type=UsageSourceType.LLM_RUN,
    source_id=str(RUN), quantity=<measured tokens>`.
    """
    resolved_source = source_id if source_id is not None else str(APPLICATION)
    idempotency_key = build_usage_idempotency_key(
        entitlement_key=entitlement_key, source_type=source_type, source_id=resolved_source)
    fields = {
        "id": usage_event_id(idempotency_key),
        "user_id": USER,
        "entitlement_key": entitlement_key,
        "quantity": quantity,
        "source_type": source_type,
        "source_id": resolved_source,
        "occurred_at": NOW,
        "billing_period": "2026-03",
        "idempotency_key": idempotency_key,
        "detail": None,
    }
    fields.update(overrides)
    return UsageEvent(**fields)


def a_subscription_event(*, outcome=SubscriptionEventOutcome.APPLIED, **overrides):
    """One processed billing webhook, `APPLIED` and attributed to `USER`/`SUBSCRIPTION`.

    Its id derives from `(provider, external_event_id)` (`subscription_event_id`), so a
    redelivery of the same event lands on the one row — the ledger's idempotency. Defaults to a
    `customer.subscription.updated` recorded as `APPLIED`; an unattributable event passes
    `outcome=SubscriptionEventOutcome.IGNORED`, `user_id=None`, `detail=...`, and a stale one
    `outcome=SubscriptionEventOutcome.SUPERSEDED`. `received_at` is when the platform handled it,
    `event_at` the provider's provenance instant the out-of-order guard compares.
    """
    fields = {
        "id": SUBSCRIPTION_EVENT,
        "provider": "stripe",
        "external_event_id": "evt_test_0001",
        "event_type": "customer.subscription.updated",
        "outcome": outcome,
        "user_id": USER,
        "subscription_id": SUBSCRIPTION,
        "event_at": LATER,
        "received_at": LATER,
        "detail": None,
    }
    fields.update(overrides)
    return SubscriptionEvent(**fields)


def a_provider_subscription_state(**overrides):
    """The subscription state a verified webhook carries — a provider's object, domain-shaped.

    Defaults to `sub_test_0001` (so its derived id is `SUBSCRIPTION`) as `ACTIVE`, its price the
    `pro` plan's fixture handle (`price_fixture_pro`, what `a_plan()` seeds) and a bounded window.
    A cancellation passes `status=SubscriptionStatus.CANCELED`; a price no plan carries passes
    `plan_external_price_id="price_unmapped"` to drive the IGNORED path.
    """
    fields = {
        "external_subscription_id": "sub_test_0001",
        "status": SubscriptionStatus.ACTIVE,
        "external_customer_id": "cus_fixture_0001",
        "plan_external_price_id": "price_fixture_pro",
        "current_period_start": NOW,
        "current_period_end": datetime(2026, 3, 31, 9, 30, tzinfo=UTC),
        "cancel_at_period_end": False,
    }
    fields.update(overrides)
    return ProviderSubscriptionState(**fields)


def a_normalized_event(*, subscription=..., **overrides):
    """A signature-verified webhook, normalized — what a `BillingProvider` hands the service.

    Defaults to a `customer.subscription.updated` for `SUBSCRIPTION`'s handle, echoing `USER` back
    as `client_user_id` (the account the checkout attributed the subscription to) and dated `LATER`
    with sequence 2, so it supersedes a subscription seeded at `NOW`/1. Its derived event id is
    `SUBSCRIPTION_EVENT`. A verified event the platform does not act on passes `subscription=None`
    (a type carrying no state); pass a built `a_provider_subscription_state(...)` to vary the state.
    """
    resolved = a_provider_subscription_state() if subscription is ... else subscription
    fields = {
        "provider": "stripe",
        "external_event_id": "evt_test_0001",
        "event_type": "customer.subscription.updated",
        "event_at": LATER,
        "event_sequence": 2,
        "subscription": resolved,
        "client_user_id": USER,
    }
    fields.update(overrides)
    return NormalizedWebhookEvent(**fields)



