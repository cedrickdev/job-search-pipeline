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
    UserId,
    candidate_document_id,
    chat_action_execution_id,
    chat_action_proposal_id,
    chat_message_id,
    document_version_id,
    interview_answer_evaluation_id,
    interview_answer_id,
    interview_question_id,
    interview_session_summary_id,
    provider_session_id,
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



