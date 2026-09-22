# tests/test_v2_llm_factory.py
"""The one allowed provider-type switch, and the composition built on it.

`LLMProviderFactory.create` is the single place that turns a stored `LLMConnection`
into a live `LLMProvider`, and the single place a credential is decrypted. These
tests pin both: the type dispatch, the credential flowing to the adapter's
Authorization header (end to end through `httpx.MockTransport`, no network), and the
refusals — a missing model, a credential no cipher can read, a stored key with no
cipher configured.
"""
import httpx
import pytest
from pydantic import SecretStr

from backend.app.llm.bootstrap import build_llm_provider_registry
from backend.app.llm.capabilities import Capability
from backend.app.llm.connection import LLMProviderType
from backend.app.llm.contracts import LLMMessage, LLMRequest
from backend.app.llm.factory import LLMProviderFactory, connection_provider_key
from backend.app.llm.failures import LLMError, LLMFailureCode
from backend.app.llm.providers.claude_code import ClaudeCodeProvider
from backend.app.llm.providers.codex import CodexProvider
from backend.app.llm.providers.openai_compatible import OpenAICompatibleProvider
from backend.app.llm.secrets import SecretCipher, SecretDecryptionError
from tests.v2_builders import CONNECTION, OTHER_CONNECTION, an_llm_connection


class _EchoCipher:
    """A trivial in-memory `SecretCipher` — no key ceremony for a unit test.

    Prefixes rather than encrypts; enough to prove the factory decrypts at version 1
    and refuses a version it holds no key for. `runtime_checkable` conformance is what
    lets it stand in for `FernetSecretCipher`.
    """

    version = 1

    def encrypt(self, plaintext: SecretStr) -> str:
        return "enc:" + plaintext.get_secret_value()

    def decrypt(self, ciphertext: str, *, secret_version: int) -> SecretStr:
        if secret_version != self.version or not ciphertext.startswith("enc:"):
            raise SecretDecryptionError(secret_version)
        return SecretStr(ciphertext[len("enc:"):])


def test_the_echo_cipher_satisfies_the_protocol():
    assert isinstance(_EchoCipher(), SecretCipher)


def test_create_builds_the_claude_code_adapter():
    connection = an_llm_connection(
        provider_type=LLMProviderType.CLAUDE_CODE, display_name="Claude Code",
        base_url=None, model=None, encrypted_api_key=None, secret_version=None)
    provider = LLMProviderFactory().create(connection)
    assert isinstance(provider, ClaudeCodeProvider)
    assert provider.metadata.provider_key == "claude_code"


def test_create_builds_the_codex_adapter():
    connection = an_llm_connection(
        provider_type=LLMProviderType.CODEX, display_name="Codex",
        base_url=None, model=None, encrypted_api_key=None, secret_version=None)
    provider = LLMProviderFactory().create(connection)
    assert isinstance(provider, CodexProvider)
    assert provider.metadata.provider_key == "codex"


def test_create_builds_a_per_connection_openai_compatible_adapter():
    # Keyless: the dispatch and the per-connection key are what this pins; the
    # credential path is proven end to end below.
    connection = an_llm_connection(encrypted_api_key=None, secret_version=None)
    provider = LLMProviderFactory().create(connection)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.metadata.provider_key == connection_provider_key(connection)
    assert provider.metadata.provider_key.startswith("conn_")
    assert Capability.STRUCTURED_OUTPUT in provider.metadata.capabilities


def test_an_api_connection_without_a_model_is_refused():
    connection = an_llm_connection(model=None,
                                   encrypted_api_key=None, secret_version=None)
    with pytest.raises(LLMError) as caught:
        LLMProviderFactory().create(connection)
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED


def test_a_stored_credential_with_no_cipher_is_refused():
    connection = an_llm_connection(encrypted_api_key="enc:whatever", secret_version=1)
    with pytest.raises(LLMError) as caught:
        LLMProviderFactory(cipher=None).create(connection)
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED


def test_an_undecryptable_credential_is_refused_without_leaking():
    connection = an_llm_connection(encrypted_api_key="tampered", secret_version=1)
    with pytest.raises(LLMError) as caught:
        LLMProviderFactory(cipher=_EchoCipher()).create(connection)
    assert caught.value.code is LLMFailureCode.PROVIDER_MISCONFIGURED
    assert "tampered" not in caught.value.detail


@pytest.mark.asyncio
async def test_the_decrypted_key_reaches_the_authorization_header():
    """End to end: the ciphertext is decrypted and sent as a bearer token, once.

    Proof the whole credential path works — the factory decrypts, the adapter carries
    it — without a real request, through the `MockTransport` seam the adapter accepts.
    """
    cipher = _EchoCipher()
    connection = an_llm_connection(
        encrypted_api_key=cipher.encrypt(SecretStr("sk-topsecret-value")),
        secret_version=1)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "hello"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    factory = LLMProviderFactory(cipher=cipher,
                                 http_transport=httpx.MockTransport(handler))
    provider = factory.create(connection)
    response = await provider.generate(
        LLMRequest(messages=(LLMMessage.user("hi"),)))
    assert response.text == "hello"
    assert seen["authorization"] == "Bearer sk-topsecret-value"


def test_build_registry_registers_each_connection():
    api = an_llm_connection(encrypted_api_key=None, secret_version=None)
    registry = build_llm_provider_registry(
        [api,
         an_llm_connection(
             id=OTHER_CONNECTION, provider_type=LLMProviderType.CLAUDE_CODE,
             display_name="Claude Code", base_url=None, model=None,
             encrypted_api_key=None, secret_version=None)])
    assert set(registry.provider_keys) == {
        connection_provider_key(api), "claude_code"}


def test_build_registry_dedupes_a_repeated_cli_type():
    """Two Claude Code connections both answer to `claude_code`; the first wins."""
    first = an_llm_connection(
        provider_type=LLMProviderType.CLAUDE_CODE, display_name="A",
        base_url=None, model=None, encrypted_api_key=None, secret_version=None)
    second = an_llm_connection(
        id=OTHER_CONNECTION, provider_type=LLMProviderType.CLAUDE_CODE,
        display_name="B", base_url=None, model=None,
        encrypted_api_key=None, secret_version=None)
    registry = build_llm_provider_registry([first, second])
    assert registry.provider_keys == ("claude_code",)
