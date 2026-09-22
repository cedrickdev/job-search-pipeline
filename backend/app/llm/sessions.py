"""A generalized provider session — where a resumable exchange keeps its handle.

V1 stored a `claude_session_id` next to a conversation, which is exactly the leak §4
names: a provider-specific column in a generic table. `ProviderSession` is the
replacement — one row per `(connection, conversation)` that holds whatever handle the
provider uses to continue an exchange, under the neutral name `external_session_id`.
The Claude CLI's `--resume` id lives here; a stateless provider simply never sets it.

The id is derived from the connection and the conversation key
(`provider_session_id`), so resuming a conversation refreshes the one session row
rather than appending a second — the idempotence the CLI's stale-session recovery
depends on. `user_id` scopes it: a session is read for its owner, never by id alone,
so one user cannot resume another's provider-side conversation.
"""
from datetime import datetime
from typing import Annotated, Self

from pydantic import Field, model_validator

from backend.app.domain.identifiers import (
    LLMConnectionId,
    ProviderSessionId,
    UserId,
    provider_session_id,
)
from backend.app.llm.contracts import LLMValue, SessionContext, TaskPurpose


class ProviderSession(LLMValue):
    """The provider-side handle for one logical conversation on one connection (§4).

    `conversation_key` is the caller's stable name for the exchange — a chat id, a
    prep-session key — and `external_session_id` is the provider's own handle for it,
    nullable because a first turn has not been issued one yet and a stateless provider
    never will be. `purpose` records what the exchange is for, so telemetry and a
    settings page can group sessions the same way runs are grouped.

    `id` is expected to equal `provider_session_id(connection_id, conversation_key)`;
    the validator holds that so a row cannot be stored under a key that would not be
    found again on resume.
    """

    id: ProviderSessionId
    user_id: UserId
    connection_id: LLMConnectionId
    conversation_key: Annotated[str, Field(min_length=1)]
    purpose: TaskPurpose = TaskPurpose.GENERIC
    external_session_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _id_is_derived_from_its_key(self) -> Self:
        expected = provider_session_id(self.connection_id, self.conversation_key)
        if self.id != expected:
            raise ValueError(
                "a ProviderSession id must be provider_session_id(connection_id, "
                "conversation_key), so a resume finds the row it wrote")
        return self

    def as_context(self) -> SessionContext:
        """The `SessionContext` a request carries to ask this session be resumed."""
        return SessionContext(external_session_id=self.external_session_id)
