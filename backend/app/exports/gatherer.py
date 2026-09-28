"""Assemble one account's portable, secret-free data into a serializable payload (§23-24).

The `AccountExportGatherer` is to the export feature what `ChatContextBuilder` is to chat: it
holds only user-scoped read repositories and reads every section `WHERE user_id = ?`, so the
payload it produces can only ever contain the requesting account's own rows. Where the chat
snapshot is deliberately tiny (a prompt paid for every turn), the export is deliberately
whole — "give me everything you hold about me" — so it draws on far more stores, but the same
two disciplines hold and are the point of the type:

- **User-isolated (§23).** Every read takes `user_id` first. There is no unscoped read on this
  builder, so "do not silently export data owned by another user" is a property of the reads it
  is *able* to make, not a rule the caller must remember. Shared facts an owned row points at
  (an `Opportunity`, a `Company`) are not re-exported — an application already names its target
  by id, and a posting belongs to no one.
- **Secret-free (§24).** The payload carries a user's *data*, never a *credential*. Three things
  make that hold rather than hope: the account is dumped with `password_hash` excluded; a stored
  LLM connection is reduced to explicit metadata (`provider`, `model`, `has_api_key`) that names
  no ciphertext, key version or auth header; and sessions are not exported at all. On top of
  those, `_assert_no_secrets` walks the finished payload and *fails the export* if any key from a
  denylist of credential field names is present — a fail-closed backstop so a future field that
  reintroduces a secret breaks the export loudly rather than leaking it quietly.

The gatherer produces a plain, JSON-ready `dict` (`model_dump(mode="json")` on every domain
value), not bytes: serialization, storage and the export's lifecycle are the service's job
(§25). It reads clocks through no side channel — `generated_at` is passed in, like every
time-dependent path in the codebase — so the payload a test builds is fully determined by its
inputs.
"""
from datetime import datetime
from typing import Final

from backend.app.domain.account_export import ACCOUNT_EXPORT_SCHEMA_VERSION
from backend.app.domain.identifiers import UserId
from backend.app.llm.connection import LLMConnection
from backend.app.repositories.contracts import (
    ApplicationOutcomeRepository,
    ApplicationPolicyRepository,
    ApplicationRepository,
    CandidateDocumentRepository,
    CandidateProfileRepository,
    CareerRecommendationRepository,
    ChatMessageRepository,
    ConversationRepository,
    InterviewSessionRepository,
    InterviewSessionSummaryRepository,
    LLMConnectionRepository,
    RoleClassificationRepository,
    SearchProfileRepository,
    StrategyChangeExecutionRepository,
    StrategyChangeProposalRepository,
    SubscriptionEventRepository,
    SubscriptionRepository,
    UsageEventRepository,
    UserRepository,
)

# How many rows each owned section may carry. Generous — an export is the whole account, not a
# prompt — but still bounded: the payload is held in memory and serialized in one pass, and no
# repository offers keyset paging, so an unbounded read is neither available nor wise. A section
# that reaches this cap is truncated to its most recent rows; the bound is stated here rather
# than left implicit so the truncation is an honest, documented limit and not a silent surprise.
_EXPORT_SECTION_LIMIT: Final[int] = 1000
# How many turns of each conversation to include. Chat threads are the one place a single owned
# parent can hold a large child collection, so its cap is separate and applied per conversation.
_EXPORT_MESSAGES_PER_CONVERSATION: Final[int] = 2000

# Field names that must never appear anywhere in an export payload (§24). Matched exactly, not by
# substring, so a safe metadata field like `has_api_key` is untouched while the ciphertext field
# `encrypted_api_key` is caught. This is the fail-closed backstop, not the primary defence: the
# gatherer already excludes each of these at the point it dumps a model, and this sweep exists so
# a *future* change that reintroduces one breaks the export rather than shipping a secret.
_SECRET_KEY_DENYLIST: Final[frozenset[str]] = frozenset({
    "password_hash",
    "encrypted_api_key",
    "secret_version",
    "token_digest",
    "csrf_token_digest",
    "master_key",
    "previous_keys",
    "webhook_secret",
    "signing_secret",
})


class ExportContainsSecret(RuntimeError):
    """A payload about to be handed out contained a denylisted credential field (§24).

    Raised by `_assert_no_secrets` so a leak is a failed export a caller must fix, never bytes a
    user receives. Names the offending key so the fix — exclude it at its source — is obvious.
    """

    def __init__(self, key: str) -> None:
        super().__init__(
            f"account export payload contains a denylisted secret field {key!r}; "
            "exclude it where the section is assembled")
        self.key = key


def _assert_no_secrets(value: object, *, _path: str = "$") -> None:
    """Walk a JSON-shaped structure and raise if any denylisted key is present (§24).

    Recurses through dicts and lists — the only containers `model_dump(mode="json")` produces —
    checking every dict key against `_SECRET_KEY_DENYLIST`. Scalars are leaves and safe: the
    denylist is about *field names* a credential rides in, not values, because a value cannot be
    recognised as a secret in general but the field that would carry one can. Fail-closed: the
    first offending key raises `ExportContainsSecret`, so no partially-swept payload escapes.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _SECRET_KEY_DENYLIST:
                raise ExportContainsSecret(key)
            _assert_no_secrets(item, _path=f"{_path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_secrets(item, _path=f"{_path}[{index}]")


def _connection_metadata(connection: LLMConnection) -> dict[str, object]:
    """The safe, explicit metadata of one stored LLM connection (§24).

    Built field by field rather than by dumping the model, so the ciphertext (`encrypted_api_key`),
    its key version (`secret_version`) and any credential-bearing `custom_headers` can never ride
    along: adding a field to `LLMConnection` does not silently add it here. `has_api_key` — a bool,
    the same thing the API tells a client — records *that* a key is configured without exporting
    it, which is exactly what a user re-importing their connections elsewhere needs to know.
    """
    return {
        "id": str(connection.id),
        "provider_type": connection.provider_type.value,
        "display_name": connection.display_name,
        "base_url": connection.base_url,
        "model": connection.model,
        "has_api_key": connection.has_api_key,
        "enabled": connection.enabled,
        "is_default": connection.is_default,
        "priority": connection.priority,
        "created_at": connection.created_at.isoformat(),
        "updated_at": connection.updated_at.isoformat(),
    }


class AccountExportGatherer:
    """Assembles one account's portable, secret-free export payload from user-scoped reads.

    Holds the read-side repositories an export draws on and nothing that could read another
    account's rows or a secret store: there is no `SessionRepository` here, and the one store
    that holds ciphertext (`LLMConnectionRepository`) is read only through `_connection_metadata`,
    which never dumps the key. Every collaborator is keyword-only and required, so a section is
    never silently omitted because a repository was forgotten at the wiring site — an export that
    quietly dropped a user's applications would be a data-loss bug wearing a success message.
    """

    def __init__(self, *,
                 users: UserRepository,
                 profiles: CandidateProfileRepository,
                 searches: SearchProfileRepository,
                 applications: ApplicationRepository,
                 outcomes: ApplicationOutcomeRepository,
                 documents: CandidateDocumentRepository,
                 role_classifications: RoleClassificationRepository,
                 recommendations: CareerRecommendationRepository,
                 strategy_proposals: StrategyChangeProposalRepository,
                 strategy_executions: StrategyChangeExecutionRepository,
                 conversations: ConversationRepository,
                 chat_messages: ChatMessageRepository,
                 interview_sessions: InterviewSessionRepository,
                 interview_summaries: InterviewSessionSummaryRepository,
                 policies: ApplicationPolicyRepository,
                 llm_connections: LLMConnectionRepository,
                 subscriptions: SubscriptionRepository,
                 usage_events: UsageEventRepository,
                 subscription_events: SubscriptionEventRepository) -> None:
        self._users = users
        self._profiles = profiles
        self._searches = searches
        self._applications = applications
        self._outcomes = outcomes
        self._documents = documents
        self._role_classifications = role_classifications
        self._recommendations = recommendations
        self._strategy_proposals = strategy_proposals
        self._strategy_executions = strategy_executions
        self._conversations = conversations
        self._chat_messages = chat_messages
        self._interview_sessions = interview_sessions
        self._interview_summaries = interview_summaries
        self._policies = policies
        self._llm_connections = llm_connections
        self._subscriptions = subscriptions
        self._usage_events = usage_events
        self._subscription_events = subscription_events

    async def gather(self, user_id: UserId, *,
                     generated_at: datetime) -> dict[str, object]:
        """The whole portable payload for `user_id` — every section scoped to this account.

        Returns a self-describing, JSON-ready `dict`: a `schema_version` a future reader keys on,
        the `generated_at` instant the caller sampled, the `account_id`, and one entry per §23
        section. `_assert_no_secrets` is run over the finished structure as the last step, so no
        payload that tripped the credential backstop is ever returned — the guarantee holds for
        every construction path, not only the ones a test exercises.
        """
        payload: dict[str, object] = {
            "schema_version": ACCOUNT_EXPORT_SCHEMA_VERSION,
            "generated_at": generated_at.isoformat(),
            "account_id": str(user_id),
            "account": await self._account(user_id),
            "candidate_profiles": await self._profiles_section(user_id),
            "search_profiles": await self._searches_section(user_id),
            "applications": await self._applications_section(user_id),
            "application_outcomes": await self._outcomes_section(user_id),
            "documents": await self._documents_section(user_id),
            "career_intelligence": await self._career_section(user_id),
            "interview_history": await self._interview_section(user_id),
            "chat_history": await self._chat_section(user_id),
            "settings": await self._settings_section(user_id),
            "billing": await self._billing_section(user_id),
        }
        _assert_no_secrets(payload)
        return payload

    async def _account(self, user_id: UserId) -> dict[str, object] | None:
        """The account itself, with `password_hash` excluded at the dump (§24).

        The credential is dropped where the model is serialized, not scrubbed afterwards, so the
        hash is never in the structure at all. Everything else — email, display name, status, the
        onboarding and login timestamps — is the user's own account data and is theirs to take.
        """
        user = await self._users.get(user_id)
        if user is None:
            return None
        return user.model_dump(mode="json", exclude={"password_hash"})

    async def _profiles_section(self, user_id: UserId) -> list[dict[str, object]]:
        """Candidate profiles, each the full aggregate — evidence and claims included (§23).

        A profile carries its own evidence store and the claims resting on it (Phase 10), so one
        dump is the whole "what the platform knows about me", which is the heart of an export.
        """
        profiles = await self._profiles.list_for_user(user_id, limit=_EXPORT_SECTION_LIMIT)
        return [profile.model_dump(mode="json") for profile in profiles]

    async def _searches_section(self, user_id: UserId) -> list[dict[str, object]]:
        searches = await self._searches.list_for_user(user_id, limit=_EXPORT_SECTION_LIMIT)
        return [search.model_dump(mode="json") for search in searches]

    async def _applications_section(self, user_id: UserId) -> list[dict[str, object]]:
        applications = await self._applications.list_for_user(
            user_id, limit=_EXPORT_SECTION_LIMIT)
        return [application.model_dump(mode="json") for application in applications]

    async def _outcomes_section(self, user_id: UserId) -> list[dict[str, object]]:
        outcomes = await self._outcomes.list_for_user(user_id, limit=_EXPORT_SECTION_LIMIT)
        return [outcome.model_dump(mode="json") for outcome in outcomes]

    async def _documents_section(self, user_id: UserId) -> list[dict[str, object]]:
        """Candidate documents, each the aggregate with its version history (§23).

        A document version points at its rendered artifact by reference, not by bytes, so this is
        metadata — dates, types, the evidence a version was built from — never the PDF itself,
        which lives in the document artifact store and is not part of this archive.
        """
        documents = await self._documents.list_for_user(user_id, limit=_EXPORT_SECTION_LIMIT)
        return [document.model_dump(mode="json") for document in documents]

    async def _career_section(self, user_id: UserId) -> dict[str, object]:
        """The career-intelligence loop's user-owned records (§23).

        The role-family verdicts, the evidence-backed recommendations, the strategy proposals and
        — for each proposal — its execution audit if it ran. The executions are fetched per
        proposal because they are keyed on the proposal id, so the read is bounded by the proposal
        cap rather than a scan.
        """
        classifications = await self._role_classifications.list_for_user(
            user_id, limit=_EXPORT_SECTION_LIMIT)
        recommendations = await self._recommendations.list_for_user(
            user_id, limit=_EXPORT_SECTION_LIMIT)
        proposals = await self._strategy_proposals.list_for_user(
            user_id, limit=_EXPORT_SECTION_LIMIT)
        executions = []
        for proposal in proposals:
            execution = await self._strategy_executions.get(user_id, proposal.id)
            if execution is not None:
                executions.append(execution.model_dump(mode="json"))
        return {
            "role_classifications": [c.model_dump(mode="json") for c in classifications],
            "recommendations": [r.model_dump(mode="json") for r in recommendations],
            "strategy_proposals": [p.model_dump(mode="json") for p in proposals],
            "strategy_executions": executions,
        }

    async def _interview_section(self, user_id: UserId) -> dict[str, object]:
        """Interview-practice sessions and their closing coaching summaries (§23)."""
        sessions = await self._interview_sessions.list_for_user(
            user_id, limit=_EXPORT_SECTION_LIMIT)
        summaries = await self._interview_summaries.list_for_user(
            user_id, limit=_EXPORT_SECTION_LIMIT)
        return {
            "sessions": [s.model_dump(mode="json") for s in sessions],
            "summaries": [s.model_dump(mode="json") for s in summaries],
        }

    async def _chat_section(self, user_id: UserId) -> list[dict[str, object]]:
        """Career-chat threads, each with its turns (§23).

        Archived threads included — an export is the whole history, not the active view — and each
        conversation carries its own messages, read through the user-scoped
        `list_for_conversation` so a thread that is not this account's yields nothing rather than
        another user's turns. Bounded per thread by `_EXPORT_MESSAGES_PER_CONVERSATION`.
        """
        conversations = await self._conversations.list_for_user(
            user_id, include_archived=True, limit=_EXPORT_SECTION_LIMIT)
        threads: list[dict[str, object]] = []
        for conversation in conversations:
            messages = await self._chat_messages.list_for_conversation(
                user_id, conversation.id, limit=_EXPORT_MESSAGES_PER_CONVERSATION)
            threads.append({
                "conversation": conversation.model_dump(mode="json"),
                "messages": [m.model_dump(mode="json") for m in messages],
            })
        return threads

    async def _settings_section(self, user_id: UserId) -> dict[str, object]:
        """The account's own configuration: application policies and LLM connections (§23-24).

        The connections are reduced to safe metadata by `_connection_metadata` — never the stored
        ciphertext — so "export my settings" hands back what the user configured without handing
        back the key that authenticates it.
        """
        policies = await self._policies.list_for_user(user_id, limit=_EXPORT_SECTION_LIMIT)
        connections = await self._llm_connections.list_for_user(
            user_id, limit=_EXPORT_SECTION_LIMIT)
        return {
            "application_policies": [p.model_dump(mode="json") for p in policies],
            "llm_connections": [_connection_metadata(c) for c in connections],
        }

    async def _billing_section(self, user_id: UserId) -> dict[str, object]:
        """Subscription, usage and processed-webhook metadata for this account (§23).

        The commercial side of the account: which plans it has subscribed to, the authoritative
        usage ledger its quotas are measured against, and the processed-webhook audit attributed
        to it. All user-scoped reads; a provider secret is never part of any of these rows.
        """
        subscriptions = await self._subscriptions.list_for_user(
            user_id, limit=_EXPORT_SECTION_LIMIT)
        usage_events = await self._usage_events.list_for_user(
            user_id, limit=_EXPORT_SECTION_LIMIT)
        subscription_events = await self._subscription_events.list_for_user(
            user_id, limit=_EXPORT_SECTION_LIMIT)
        return {
            "subscriptions": [s.model_dump(mode="json") for s in subscriptions],
            "usage_events": [e.model_dump(mode="json") for e in usage_events],
            "subscription_events": [e.model_dump(mode="json") for e in subscription_events],
        }
