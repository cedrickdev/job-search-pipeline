"""The one place a stored connection becomes a live provider (§14, §67).

`LLMProviderFactory.create` is the *only* function in the platform allowed to branch
on `LLMProviderType` — it turns a persisted `LLMConnection` into the concrete
`LLMProvider` its type names, decrypting the stored credential at that instant and
never before. Everything above it holds the neutral `LLMProvider` contract, which is
what keeps the "no `if provider == …` in business code" rule (§3) true: the one
switch that must exist is quarantined here.

Two security properties live at this boundary:

- **The credential is decrypted for the instant a provider is built, and no longer.**
  The plaintext key never sits in a field, a log or a response — the factory reads the
  ciphertext off the connection, asks the `SecretCipher` for the plaintext, and hands
  it straight to the adapter as a `SecretStr` (§21). A connection with no stored key
  (a CLI, a keyless local server) yields a provider with no credential.
- **A CLI provider is built without a base URL or a key.** `ClaudeCodeProvider` and
  `CodexProvider` take neither, so there is no path here that could inject the
  `ANTHROPIC_*` credential the §1 invariant forbids — the model already refused to
  store one, and the factory never invents one.
"""
import httpx
from pydantic import SecretStr

from backend.app.llm.connection import LLMConnection, LLMProviderType
from backend.app.llm.contracts import LLMProvider
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.providers.claude_code import ClaudeCodeProvider
from backend.app.llm.providers.codex import CodexProvider
from backend.app.llm.providers.openai_compatible import OpenAICompatibleProvider
from backend.app.llm.secrets import SecretCipher, SecretDecryptionError


def connection_provider_key(connection: LLMConnection) -> str:
    """The stable `provider_key` a connection's live provider answers to.

    A CLI connection maps to the CLI's own fixed key (`claude_code`, `codex`), because
    the provider identity is the CLI itself and a user has one of each. An
    OpenAI-compatible connection maps to a per-connection key, because a user may
    configure several gateways and each is a distinct provider the registry must keep
    apart. The `conn_` prefix guarantees the key starts with a letter — a bare UUID hex
    can start with a digit, which the `provider_key` pattern forbids.
    """
    if connection.provider_type is LLMProviderType.CLAUDE_CODE:
        return "claude_code"
    if connection.provider_type is LLMProviderType.CODEX:
        return "codex"
    return f"conn_{connection.id.hex}"


class LLMProviderFactory:
    """Builds a live `LLMProvider` from a stored `LLMConnection` (§67).

    Holds the `SecretCipher` used to decrypt a stored credential and, for the API
    adapters, an optional `http_transport` — the `httpx.MockTransport` seam the tests
    substitute so no network call is made. The CLI adapters take neither.
    """

    def __init__(self, *, cipher: SecretCipher | None = None,
                 http_transport: "httpx.AsyncBaseTransport | None" = None) -> None:
        self._cipher = cipher
        self._http_transport = http_transport

    def create(self, connection: LLMConnection) -> LLMProvider:
        """The live provider for one connection — the one allowed type switch.

        Raises `PROVIDER_MISCONFIGURED` when a connection cannot be built into a
        provider: an API connection with no model, or a stored credential the
        configured cipher cannot decrypt.
        """
        provider_type = connection.provider_type
        if provider_type is LLMProviderType.CLAUDE_CODE:
            return ClaudeCodeProvider()
        if provider_type is LLMProviderType.CODEX:
            return CodexProvider()
        # The two remaining members are the OpenAI-compatible transports; both build
        # the same adapter, which sets its local/remote capability from the validated
        # address rather than the type name.
        return self._openai_compatible(connection)

    def _openai_compatible(self, connection: LLMConnection) -> OpenAICompatibleProvider:
        if not connection.model:
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail="an OpenAI-compatible connection needs a model")
        # The model guarantees an API connection carries a base_url; assert it so the
        # type narrows rather than passing `str | None` to a `str` parameter.
        assert connection.base_url is not None
        return OpenAICompatibleProvider(
            provider_key=connection_provider_key(connection),
            display_name=connection.display_name,
            base_url=connection.base_url,
            default_model=connection.model,
            transport=connection.transport,
            api_key=self._decrypt(connection),
            extra_headers=connection.custom_headers,
            priority=connection.priority,
            http_transport=self._http_transport)

    def _decrypt(self, connection: LLMConnection) -> SecretStr | None:
        """The connection's plaintext credential, or `None` when it stores none."""
        if connection.encrypted_api_key is None:
            return None
        if self._cipher is None:
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail="no secret cipher is configured to decrypt the stored "
                       "credential")
        # The model's secret-pair invariant guarantees a version accompanies the
        # ciphertext; assert it so the type narrows to `int`.
        assert connection.secret_version is not None
        try:
            return self._cipher.decrypt(connection.encrypted_api_key,
                                        secret_version=connection.secret_version)
        except SecretDecryptionError as exc:
            # The message names no key material; a wrong or rotated-away master key,
            # or a tampered ciphertext, is a configuration problem for the operator.
            raise LLMError(
                LLMFailureCode.PROVIDER_MISCONFIGURED,
                detail="the stored credential could not be decrypted") from exc
