"""A user's configured way to reach an LLM provider, stored (Phase 11 §4).

An `LLMConnection` is the persisted half of the platform: the router runs providers,
and a connection is what a user configured for one of them — which provider type, at
which base URL, with which model, and (for a hosted gateway) an API key kept
encrypted at rest. `backend.app.llm.factory` turns a connection into a live
`LLMProvider`; the settings API turns a form into a connection.

Two rules are enforced here rather than left to the API, because a value object that
can be constructed wrong is one a migration or a test will eventually construct wrong:

- **A CLI connection carries no credential and no base URL.** Claude Code and Codex
  authenticate themselves and run a local binary; injecting a key or a URL would both
  be meaningless and, for the `ANTHROPIC_*` case, violate the Phase 11 security
  invariant (§1). The model refuses it, so there is no code path that stores one.
- **An API connection carries a base URL.** An OpenAI-compatible endpoint cannot be
  reached without one, and §5 forbids assuming the hostname — so it is required and
  never defaulted.

The credential itself is never a field in plaintext. `encrypted_api_key` is the
ciphertext `backend.app.llm.secrets` produced and `secret_version` tags which key
made it; the two are both-or-neither. The plaintext exists only for the instant the
factory decrypts it to build a provider, and the API surfaces `has_api_key`, never
the value (§13).
"""
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, model_validator

from backend.app.domain.identifiers import LLMConnectionId, UserId
from backend.app.llm.contracts import LLMValue, ProviderTransport

# The default priority a connection is created with, mirroring
# `LLMProviderMetadata.priority`: lower runs first, and a user promotes a connection
# by lowering it. 100 leaves room on either side without forcing a choice at creation.
DEFAULT_CONNECTION_PRIORITY: Final = 100


class LLMProviderType(StrEnum):
    """Which adapter a connection is built into — the closed set Phase 11 implements.

    Dispatched on by `backend.app.llm.factory` alone, and nowhere else: the factory is
    the one place allowed to know a provider type maps to a concrete adapter, which is
    what keeps the "no `if provider == …` in business code" rule (§3) true. Later
    phases add native Anthropic/Gemini adapters here (§105); Phase 11 ships the CLI
    pair and the two OpenAI-compatible transports.
    """

    CLAUDE_CODE = "CLAUDE_CODE"
    CODEX = "CODEX"
    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"
    LOCAL_OPENAI_COMPATIBLE = "LOCAL_OPENAI_COMPATIBLE"

    @property
    def transport(self) -> ProviderTransport:
        """The transport family this type reaches its model over."""
        return _TRANSPORT_BY_TYPE[self]

    @property
    def is_cli(self) -> bool:
        """Whether this type runs a local binary and manages its own auth."""
        return self in _CLI_TYPES

    @property
    def is_api(self) -> bool:
        """Whether this type speaks HTTP to an endpoint and may carry a credential."""
        return not self.is_cli


# CLI types manage their own auth and run a local binary; API types speak HTTP. The
# split is a fact about the transport, stated once so both the validator below and
# the factory read the same source rather than each re-deciding.
_CLI_TYPES: Final[frozenset[LLMProviderType]] = frozenset({
    LLMProviderType.CLAUDE_CODE, LLMProviderType.CODEX,
})

_TRANSPORT_BY_TYPE: Final[dict[LLMProviderType, ProviderTransport]] = {
    LLMProviderType.CLAUDE_CODE: ProviderTransport.CLI,
    LLMProviderType.CODEX: ProviderTransport.CLI,
    LLMProviderType.OPENAI_COMPATIBLE: ProviderTransport.OPENAI_COMPATIBLE_API,
    LLMProviderType.LOCAL_OPENAI_COMPATIBLE: ProviderTransport.LOCAL_OPENAI_COMPATIBLE,
}


class LLMConnection(LLMValue):
    """One stored connection to an LLM provider, owned by one user (§4).

    Frozen and closed like every value in the contract. `user_id` makes it
    authorization-scoped by construction: a connection is read `WHERE user_id = ?`,
    never by id alone. `is_default` marks the one a task uses when a user stated no
    preference; `enabled` is the on/off a settings page toggles without deleting the
    row (and its stored key).

    `priority` orders a user's own connections when more than one could serve — the
    per-user echo of `LLMProviderMetadata.priority`. `custom_headers` rides only on an
    API connection (an organization header, a gateway route); a CLI connection has
    nowhere to put one, which the validator enforces.
    """

    id: LLMConnectionId
    user_id: UserId
    provider_type: LLMProviderType
    display_name: Annotated[str, Field(min_length=1)]
    base_url: str | None = None
    model: str | None = None
    # The ciphertext `SecretCipher.encrypt` produced, and the version tag naming the
    # key that made it — both-or-neither. Never the plaintext; that lives only for the
    # instant the factory decrypts it (§21).
    encrypted_api_key: str | None = None
    secret_version: Annotated[int, Field(ge=1)] | None = None
    custom_headers: Mapping[str, str] = Field(default_factory=dict)
    enabled: bool = True
    is_default: bool = False
    priority: Annotated[int, Field(ge=0)] = DEFAULT_CONNECTION_PRIORITY
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _secret_pair_is_complete(self) -> Self:
        if (self.encrypted_api_key is None) != (self.secret_version is None):
            raise ValueError(
                "encrypted_api_key and secret_version are stored together or not "
                "at all")
        return self

    @model_validator(mode="after")
    def _transport_shape_is_coherent(self) -> Self:
        if self.provider_type.is_cli:
            # A CLI provider authenticates itself and runs a local binary: a base URL
            # is meaningless and a stored credential would violate §1's rule that the
            # platform never injects one. Refused at construction so no store holds it.
            if self.base_url is not None:
                raise ValueError(
                    f"a {self.provider_type} connection has no base_url")
            if self.encrypted_api_key is not None:
                raise ValueError(
                    f"a {self.provider_type} connection stores no API key; the CLI "
                    "manages its own authentication")
            if self.custom_headers:
                raise ValueError(
                    f"a {self.provider_type} connection carries no custom headers")
        elif not self.base_url:
            # An OpenAI-compatible endpoint cannot be reached without a base URL, and
            # §5 forbids defaulting one — the hostname is not assumed to be OpenAI's.
            raise ValueError(
                f"a {self.provider_type} connection requires a base_url")
        return self

    @property
    def has_api_key(self) -> bool:
        """Whether a credential is stored — the only thing the API tells a client (§13)."""
        return self.encrypted_api_key is not None

    @property
    def transport(self) -> ProviderTransport:
        return self.provider_type.transport
