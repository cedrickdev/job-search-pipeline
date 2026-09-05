"""Typed entity identifiers.

UUIDs, as docs/ARCHITECTURE.md §4 requires, wrapped in `NewType` so mypy rejects
passing a `CompanyId` where an `OpportunityId` belongs. That costs one call at
construction and buys a class of bug that is otherwise invisible: every id has
the same runtime type, so a swapped argument in a matching or application
service would type-check and then quietly query the wrong table.

`NewType` is erased at runtime, so validation still sees a plain UUID and
Pydantic keeps accepting the usual inputs.

Most ids are random (`uuid4`). The exception is `SURROGATE_KEY_NAMESPACE` and the
one derivation built on it here: an id that must be *recomputable* from what it
belongs to cannot be random, or a retried write would produce a second row.
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
OpportunityId = NewType("OpportunityId", UUID)
MatchEvaluationId = NewType("MatchEvaluationId", UUID)
ApplicationDecisionId = NewType("ApplicationDecisionId", UUID)


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


def new_match_evaluation_id() -> MatchEvaluationId:
    return MatchEvaluationId(uuid4())


def new_application_decision_id() -> ApplicationDecisionId:
    return ApplicationDecisionId(uuid4())
