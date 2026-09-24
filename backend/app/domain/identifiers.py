"""Typed entity identifiers.

UUIDs, as docs/ARCHITECTURE.md §4 requires, wrapped in `NewType` so mypy rejects
passing a `CompanyId` where an `OpportunityId` belongs. That costs one call at
construction and buys a class of bug that is otherwise invisible: every id has
the same runtime type, so a swapped argument in a matching or application
service would type-check and then quietly query the wrong table.

`NewType` is erased at runtime, so validation still sees a plain UUID and
Pydantic keeps accepting the usual inputs.

Most ids are random (`uuid4`). The exceptions are `SURROGATE_KEY_NAMESPACE` and
the derivations built on it here: an id that must be *recomputable* from what it
belongs to cannot be random, or a retried write would produce a second row. Phase
6 leans on that hard — company discovery is expected to run repeatedly over the
same seeds, and §23 asks for idempotence by construction rather than by
convention.
"""
from typing import NewType
from uuid import UUID, uuid4, uuid5

# The namespace for every derived identifier in V2, domain and persistence alike.
# Fixed for the lifetime of the schema: changing it would orphan every row whose
# key was derived before the change, because nothing would compute those keys
# again. `backend.app.infrastructure.database.mappers` derives its child-row keys
# from this same constant, so there is one namespace rather than two that could
# drift.
SURROGATE_KEY_NAMESPACE = UUID("20b521f6-f70d-4db4-895c-2fe588adf2ce")

UserId = NewType("UserId", UUID)
UserSessionId = NewType("UserSessionId", UUID)
CandidateProfileId = NewType("CandidateProfileId", UUID)
EvidenceId = NewType("EvidenceId", UUID)
ClaimId = NewType("ClaimId", UUID)
SearchProfileId = NewType("SearchProfileId", UUID)
ApplicationPolicyId = NewType("ApplicationPolicyId", UUID)
CompanyId = NewType("CompanyId", UUID)
CompanyLocationId = NewType("CompanyLocationId", UUID)
CompanyAliasId = NewType("CompanyAliasId", UUID)
CareerSiteId = NewType("CareerSiteId", UUID)
CompanyDiscoveryRecordId = NewType("CompanyDiscoveryRecordId", UUID)
OpportunityId = NewType("OpportunityId", UUID)
GeocodingCacheEntryId = NewType("GeocodingCacheEntryId", UUID)
MatchEvaluationId = NewType("MatchEvaluationId", UUID)
EligibilityResultId = NewType("EligibilityResultId", UUID)
ApplicationDecisionId = NewType("ApplicationDecisionId", UUID)
CandidateDocumentId = NewType("CandidateDocumentId", UUID)
DocumentVersionId = NewType("DocumentVersionId", UUID)
LLMConnectionId = NewType("LLMConnectionId", UUID)
ProviderSessionId = NewType("ProviderSessionId", UUID)
LLMRunId = NewType("LLMRunId", UUID)
ApplicationId = NewType("ApplicationId", UUID)
ApplicationEventId = NewType("ApplicationEventId", UUID)
SubmissionAttemptId = NewType("SubmissionAttemptId", UUID)
ConversationId = NewType("ConversationId", UUID)
ChatMessageId = NewType("ChatMessageId", UUID)
ChatActionProposalId = NewType("ChatActionProposalId", UUID)
ChatActionExecutionId = NewType("ChatActionExecutionId", UUID)
InterviewSessionId = NewType("InterviewSessionId", UUID)
InterviewQuestionId = NewType("InterviewQuestionId", UUID)
InterviewAnswerId = NewType("InterviewAnswerId", UUID)
InterviewAnswerEvaluationId = NewType("InterviewAnswerEvaluationId", UUID)
InterviewSessionSummaryId = NewType("InterviewSessionSummaryId", UUID)


def new_user_id() -> UserId:
    return UserId(uuid4())


def new_user_session_id() -> UserSessionId:
    return UserSessionId(uuid4())


def new_candidate_profile_id() -> CandidateProfileId:
    return CandidateProfileId(uuid4())


def default_candidate_profile_id(user_id: UserId) -> CandidateProfileId:
    """The id of the profile onboarding creates for an account.

    Derived rather than random, for two reasons. A double-submitted onboarding form
    collides on the primary key instead of leaving the account with two profiles;
    and "the account's profile" becomes a value a service can compute, so reading
    it is one `get` rather than a query that has to pick between rows and quietly
    prefers the oldest.

    A genuinely second profile — a different search persona — is an explicit act
    with a fresh `new_candidate_profile_id()`, which is why the name says *default*
    rather than *the*.
    """
    return CandidateProfileId(
        uuid5(SURROGATE_KEY_NAMESPACE, f"candidate_profile:{user_id}"))


def new_evidence_id() -> EvidenceId:
    return EvidenceId(uuid4())


def new_claim_id() -> ClaimId:
    return ClaimId(uuid4())


def new_search_profile_id() -> SearchProfileId:
    return SearchProfileId(uuid4())


def new_application_policy_id() -> ApplicationPolicyId:
    return ApplicationPolicyId(uuid4())


def new_company_id() -> CompanyId:
    return CompanyId(uuid4())


def new_company_location_id() -> CompanyLocationId:
    return CompanyLocationId(uuid4())


def company_alias_id(company_id: CompanyId, normalized_alias: str) -> CompanyAliasId:
    """The id of "this company is also called that".

    Derived, so a provider that meets `LOGITECH` on every sweep records the alias
    once. `normalized_alias` — not the raw label — is the key: `Logitech SA` and
    `LOGITECH  SA` are the same claim about the same employer, and keying on the
    raw string would store both and then have to explain which is canonical.

    Callers pass the output of
    `backend.app.domain.company.normalize_company_name` — the same value
    `CompanyAlias.normalized_alias` exposes. Nothing here re-normalizes: this
    module knows about UUIDs and must not grow a dependency on the identity rules
    it would then have to keep in step.
    """
    return CompanyAliasId(
        uuid5(SURROGATE_KEY_NAMESPACE, f"company_alias:{company_id}:{normalized_alias}"))


def career_site_id(company_id: CompanyId, url: str) -> CareerSiteId:
    """The id of one careers endpoint of one company.

    A company legitimately has several — a corporate careers page, an ATS board,
    a spontaneous-application form (§11) — so the URL is what distinguishes them.
    Derived from it, so re-detecting the same board updates that row instead of
    appending a duplicate.
    """
    return CareerSiteId(
        uuid5(SURROGATE_KEY_NAMESPACE, f"career_site:{company_id}:{url}"))


def company_discovery_record_id(provider_key: str,
                                external_id: str) -> CompanyDiscoveryRecordId:
    """The id of one provider's sighting of one company.

    Keyed by the provider and *its* identifier for the employer, not by our
    `company_id`: the record is the provenance of a discovery (§5), so it must
    survive the resolution deciding which canonical company it points at. A
    sighting that was first attached to a provisional company and later re-pointed
    at the confirmed one is the same sighting, and re-running the provider must
    update it rather than record a second.
    """
    return CompanyDiscoveryRecordId(
        uuid5(SURROGATE_KEY_NAMESPACE,
              f"company_discovery_record:{provider_key}:{external_id}"))


def new_opportunity_id() -> OpportunityId:
    return OpportunityId(uuid4())


def discovered_opportunity_id(source_key: str, external_key: str) -> OpportunityId:
    """The id of an opportunity a source just handed back.

    Derived, for the same reason as `default_candidate_profile_id`: a sweep that
    runs twice an hour meets the same posting repeatedly, and a random id per
    sighting would turn one vacancy into twelve rows a day. `external_key` is
    whatever the source can promise is stable for that posting — its own id when
    it publishes one, its URL otherwise — and `source_key` scopes it, because two
    boards numbering their postings from 1 are not describing the same job.

    Deduplication *across* sources is a different question with a different
    answer: `Opportunity.dedup_fingerprint` (company + title), which V1 already
    computes and Phase 6 will sharpen.
    """
    return OpportunityId(
        uuid5(SURROGATE_KEY_NAMESPACE, f"opportunity:{source_key}:{external_key}"))


def geocoding_cache_entry_id(provider: str, country: str,
                             normalized_query: str) -> GeocodingCacheEntryId:
    """The id of one provider's answer about one normalized query.

    Derived, because the cache's whole purpose is that asking twice costs one
    call: a random key would let two enrichment runs store two rows for the same
    question and then disagree about which is current. The upsert that writes it
    is idempotent by construction, which is what Phase 7 §23 asks for.

    All three parts are in the key. The provider, because two geocoders answer
    the same question differently and caching one under the other's name would
    attribute provenance to the wrong service. The country, because "Neuchâtel"
    is a different place depending on the hint that accompanied it. And the
    *normalized* query rather than the raw one, so `"  Lausanne "` and
    `"lausanne"` are one entry — normalization is
    `backend.app.domain.geo.normalize_geocoding_query`, applied by the caller for
    the same reason `company_alias_id` does not normalize: this module knows
    about UUIDs and must not grow a dependency on rules it would then have to
    keep in step.

    An unhinted query passes the empty string for `country`, which is a distinct
    key from any two-letter code and therefore a distinct cached answer.
    """
    return GeocodingCacheEntryId(
        uuid5(SURROGATE_KEY_NAMESPACE,
              f"geocoding_cache:{provider}:{country}:{normalized_query}"))


def new_match_evaluation_id() -> MatchEvaluationId:
    return MatchEvaluationId(uuid4())


def match_evaluation_id(candidate_profile_id: CandidateProfileId,
                        opportunity_id: OpportunityId) -> MatchEvaluationId:
    """The id of the match verdict for one (profile, opportunity) pair.

    Derived rather than random, and for the same reason as its eligibility twin
    `eligibility_result_id`: `match_evaluations` keys its upsert on
    `(candidate_profile_id, opportunity_id)`, so re-scoring a pair must replace the
    previous evaluation instead of accumulating a row per run. A deterministic
    engine that meets the same pair twice therefore writes the same key, and a
    retried write after a failed flush is idempotent by construction rather than by
    the unique constraint catching it. `new_match_evaluation_id` stays for a caller
    that genuinely wants a fresh, unrelated evaluation.
    """
    return MatchEvaluationId(
        uuid5(SURROGATE_KEY_NAMESPACE,
              f"match_evaluation:{candidate_profile_id}:{opportunity_id}"))


def eligibility_result_id(candidate_profile_id: CandidateProfileId,
                          opportunity_id: OpportunityId) -> EligibilityResultId:
    """The id of the eligibility verdict for one (profile, opportunity) pair.

    Derived rather than random, for the same reason `match_evaluations` keys its
    upsert on `(candidate_profile_id, opportunity_id)`: re-evaluating a pair must
    replace the previous verdict, not accumulate a new row every run. A permit
    that changes or a pack that is corrected produces a fresh evaluation under the
    same id, so the latest answer is always the one at that key. The per-check
    rows underneath it are keyed off this id in the mapper, so a re-evaluation
    that drops a gate drops its row too.
    """
    return EligibilityResultId(
        uuid5(SURROGATE_KEY_NAMESPACE,
              f"eligibility_result:{candidate_profile_id}:{opportunity_id}"))


def new_application_decision_id() -> ApplicationDecisionId:
    return ApplicationDecisionId(uuid4())


def candidate_document_id(candidate_profile_id: CandidateProfileId,
                          opportunity_id: OpportunityId,
                          document_type: str) -> CandidateDocumentId:
    """The id of the document a candidate keeps for one posting, of one type.

    Derived rather than random, for the reason `match_evaluation_id` is: a
    `candidate_documents` row is keyed on `(candidate_profile_id, opportunity_id,
    document_type)`, so regenerating a résumé for a posting must reuse the same
    document — accruing a new *version* under it — rather than leaving a second,
    orphaned document behind. `document_type` is the `CandidateDocumentType`
    value, so a résumé and a cover letter for the same posting are two documents,
    which is exactly what they are.

    `new_candidate_document_id` is not offered: a document is always about a
    (profile, opportunity, type) triple, and a random one would be a document with
    no way to be found again.
    """
    return CandidateDocumentId(
        uuid5(SURROGATE_KEY_NAMESPACE,
              f"candidate_document:{candidate_profile_id}:{opportunity_id}"
              f":{document_type}"))


def document_version_id(document_id: CandidateDocumentId,
                        version: int) -> DocumentVersionId:
    """The id of one version of one document.

    Keyed by `(document_id, version)` — the pair the unique constraint covers — so
    a retried write of version 3 after a failed flush lands on the same row rather
    than inserting a fourth. The version number is the document's own monotonic
    counter, assigned by the service that appends it; this only turns that pair
    into a stable key.
    """
    return DocumentVersionId(
        uuid5(SURROGATE_KEY_NAMESPACE,
              f"document_version:{document_id}:{version}"))


def new_llm_connection_id() -> LLMConnectionId:
    """The id of a user's configured way to reach an LLM provider (Phase 11).

    Random, not derived: a user legitimately keeps two connections of the same
    provider type — a work OpenAI-compatible gateway and a personal one — so
    nothing about the pair `(user, provider_type)` identifies one, and a derived
    key would make the second overwrite the first.
    """
    return LLMConnectionId(uuid4())


def provider_session_id(connection_id: LLMConnectionId,
                        conversation_key: str) -> ProviderSessionId:
    """The id of one connection's session for one logical conversation.

    Derived rather than random, for the reason `default_candidate_profile_id` is:
    a conversation continued against the same connection must reuse — and refresh —
    the one session row that holds the provider's `external_session_id`, not append
    a second every time it resumes. `conversation_key` is the caller's stable handle
    for the exchange (a chat id, a prep session key); `connection_id` scopes it,
    because the same conversation resumed against a different provider is a genuinely
    different provider-side session.
    """
    return ProviderSessionId(
        uuid5(SURROGATE_KEY_NAMESPACE,
              f"provider_session:{connection_id}:{conversation_key}"))


def new_llm_run_id() -> LLMRunId:
    """The id of one telemetry record of one LLM call (Phase 11).

    Random: a run is an event, not an entity a retry should collapse onto — two
    calls for the same purpose are two rows, which is the whole point of the
    telemetry (docs/LLM_PROVIDER_ARCHITECTURE.md §12).
    """
    return LLMRunId(uuid4())


def application_id(idempotency_key: str) -> ApplicationId:
    """The id of one application, derived from its idempotency key (Phase 12 §36).

    Derived rather than random, and this is the load-bearing decision of the
    duplicate-prevention story: the id *is* a function of the idempotency key, so
    two independent attempts to open an application for the same
    `(candidate_profile, target, channel)` compute the same id and collide on the
    primary key instead of opening a second application against the same posting.
    The database's UNIQUE constraint on the key (`rev_0009`) is the second half —
    it catches a hand-written row that bypassed this function — but the derivation
    is what makes the common path idempotent by construction rather than by a
    caught error (docs/APPLICATION_ENGINE.md §36-40).

    `idempotency_key` is the value `Application.build_idempotency_key` composes; it
    is passed in rather than recomputed here for the same reason `company_alias_id`
    takes a normalized alias: this module knows about UUIDs and must not grow a
    dependency on the rules that shape the key.
    """
    return ApplicationId(uuid5(SURROGATE_KEY_NAMESPACE, f"application:{idempotency_key}"))


def new_application_event_id() -> ApplicationEventId:
    """The id of one entry in an application's append-only audit trail (§41).

    Random: an event is a fact that happened at an instant, never an entity a
    retry should collapse onto. Two "submission started" events are two facts, and
    the trail is the aggregate's own history, so each row stands on its own.
    """
    return ApplicationEventId(uuid4())


def submission_attempt_id(application: ApplicationId,
                          attempt_number: int) -> SubmissionAttemptId:
    """The id of one submission attempt against one application (§39).

    Derived from `(application, attempt_number)` — the pair the unique constraint
    covers — so a retried write of attempt 2 after a failed flush lands on the same
    row rather than inserting a third. The attempt number is the application's own
    monotonic counter, assigned by the submission service; this only turns that
    pair into a stable key, exactly as `document_version_id` does for a document's
    versions.
    """
    return SubmissionAttemptId(
        uuid5(SURROGATE_KEY_NAMESPACE,
              f"submission_attempt:{application}:{attempt_number}"))


def new_conversation_id() -> ConversationId:
    """The id of one career-chat conversation (Phase 13 §…).

    Random: a conversation is an entity a user opens, not something recomputable
    from what it holds — two "New chat" clicks are two conversations, and the
    provider-side session that resumes it keys on this id, not the other way round.
    """
    return ConversationId(uuid4())


def chat_message_id(conversation_id: ConversationId, sequence: int) -> ChatMessageId:
    """The id of the message at one position in one conversation.

    Derived from `(conversation_id, sequence)` — the pair the unique constraint
    covers — so a turn that is finalized twice after a failed flush writes the same
    message rows rather than duplicating the exchange. `sequence` is the
    conversation's own monotonic counter, assigned by the chat service as it appends;
    this only turns that pair into a stable key, exactly as `document_version_id`
    does for a document's versions.
    """
    return ChatMessageId(
        uuid5(SURROGATE_KEY_NAMESPACE, f"chat_message:{conversation_id}:{sequence}"))


def chat_action_proposal_id(message_id: ChatMessageId,
                            ordinal: int) -> ChatActionProposalId:
    """The id of the nth typed action a single assistant turn proposed.

    Derived from `(message_id, ordinal)`, for the reason `chat_message_id` is derived:
    re-finalizing the turn that produced them must land on the same proposal rows, not
    append a second copy of every proposal. `ordinal` is the action's position in the
    turn's fenced proposal block (0-based); the message it belongs to scopes it, so two
    turns proposing "submit" are two distinct proposals rather than one overwritten.
    """
    return ChatActionProposalId(
        uuid5(SURROGATE_KEY_NAMESPACE,
              f"chat_action_proposal:{message_id}:{ordinal}"))


def chat_action_execution_id(
        proposal_id: ChatActionProposalId) -> ChatActionExecutionId:
    """The id of the record of executing one proposal.

    Derived from the proposal, and this is the load-bearing half of the idempotency
    story the executor rests on: a proposal executed twice — a double-clicked
    "Confirm", a retried request — computes the same execution id and collides on the
    primary key instead of running the underlying service action a second time. The
    executor still guards on the proposal's status, but the derived id is what makes
    the common path idempotent by construction rather than by a caught race.
    """
    return ChatActionExecutionId(
        uuid5(SURROGATE_KEY_NAMESPACE, f"chat_action_execution:{proposal_id}"))


def new_interview_session_id() -> InterviewSessionId:
    """The id of one adaptive interview-practice session (Phase 14 §1).

    Random: a session is an entity a user opens to rehearse against one target, not
    something recomputable from what it holds — two "Start practice" clicks are two
    sessions, even against the same opportunity, and each keeps its own immutable
    history. A candidate who wants to practise the same role twice and compare
    readiness over time depends on that: collapsing the second onto the first would
    erase the very trajectory §37 asks the history to preserve.
    """
    return InterviewSessionId(uuid4())


def interview_question_id(session_id: InterviewSessionId,
                          sequence: int) -> InterviewQuestionId:
    """The id of the question at one position in one session.

    Derived from `(session_id, sequence)` — the pair the unique constraint covers —
    so a turn that is finalized twice after a failed flush writes the same question
    row rather than duplicating the exchange, exactly as `chat_message_id` does. The
    sequence is the session's own monotonic counter, assigned by the question engine
    as it asks; an adaptive follow-up is simply the next sequence, not a child of the
    question it follows.
    """
    return InterviewQuestionId(
        uuid5(SURROGATE_KEY_NAMESPACE, f"interview_question:{session_id}:{sequence}"))


def interview_answer_id(question_id: InterviewQuestionId) -> InterviewAnswerId:
    """The id of the candidate's accepted answer to one question.

    Derived from the question alone, because Phase 14 accepts one immutable answer
    per question (§52-53): retries are deferred, so a question has at most one
    answer and its id is a function of the question. A resubmitted answer after a
    failed flush therefore lands on the same row instead of recording the reply
    twice. When retries arrive in a later phase this becomes `(question, attempt)`,
    the way `submission_attempt_id` already keys attempts.
    """
    return InterviewAnswerId(
        uuid5(SURROGATE_KEY_NAMESPACE, f"interview_answer:{question_id}"))


def interview_answer_evaluation_id(
        answer_id: InterviewAnswerId) -> InterviewAnswerEvaluationId:
    """The id of the structured evaluation of one answer.

    Derived from the answer, so re-evaluating a reply — a provider retry, a repeated
    grade after a failed flush — replaces the one evaluation under it rather than
    accumulating a row per attempt. An answer has exactly one current evaluation:
    readiness is aggregated deterministically from these, and two evaluations of the
    same answer would let a pair be counted twice.
    """
    return InterviewAnswerEvaluationId(
        uuid5(SURROGATE_KEY_NAMESPACE, f"interview_answer_evaluation:{answer_id}"))


def interview_session_summary_id(
        session_id: InterviewSessionId) -> InterviewSessionSummaryId:
    """The id of the coaching summary produced when a session completes.

    Derived from the session, because a session has exactly one summary: completing
    it twice after a failed flush must reuse that row, not append a second report.
    The summary is the session's closing artefact — readiness, coverage, coaching —
    so keying it on the session is keying it on the thing it summarizes.
    """
    return InterviewSessionSummaryId(
        uuid5(SURROGATE_KEY_NAMESPACE, f"interview_session_summary:{session_id}"))
