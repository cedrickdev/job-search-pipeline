"""Managing a user's stored LLM connections — the write side of the settings surface.

The counterpart of `CandidateEvidenceService` for the LLM platform: the settings API
turns a form into a call here, and this service turns it into an authorization-scoped
`LLMConnection` row. Everything a route must not decide for itself lives here —

- **the owner comes from the session, never the body.** Every method takes `user_id`
  and reaches the repository through it, so a connection is created under, read by,
  and deleted from only the account that owns it. A connection id that belongs to
  another account reads as absent (§4).
- **a credential is encrypted before it is stored, and the plaintext never lingers.**
  A submitted API key is handed to the `SecretCipher` and only its ciphertext and
  version reach the row (§21); the API surfaces `has_api_key`, never the value (§13).
  A deployment with no master key configured can still manage CLI and keyless local
  connections — the service refuses *only* the write that would need a key it cannot
  encrypt, rather than failing wholesale.
- **there is exactly one default.** Marking a connection default clears the flag on the
  account's others first, in the same request, so the "which connection does a task use
  by default" question always has one answer (§4).

Health is a probe, not a stored fact (§37): `healthcheck` builds the live provider
through the same factory the router uses and asks it, returning the `ProviderHealth`
without persisting it — a settings page reads the current state rather than a stale row.
This service never runs a *generation*; there is no path here that sends a prompt, only
one that configures where a prompt could go.
"""
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from pydantic import SecretStr, ValidationError

from backend.app.domain.identifiers import (
    LLMConnectionId,
    UserId,
    new_llm_connection_id,
)
from backend.app.llm.connection import (
    DEFAULT_CONNECTION_PRIORITY,
    LLMConnection,
    LLMProviderType,
)
from backend.app.llm.contracts import ProviderHealth
from backend.app.llm.factory import LLMProviderFactory
from backend.app.llm.providers.net_policy import HostResolver
from backend.app.llm.secrets import CURRENT_SECRET_VERSION, SecretCipher
from backend.app.repositories.contracts import (
    DEFAULT_LIMIT,
    LLMConnectionRepository,
)


class LLMConnectionNotFound(Exception):
    """No connection is stored under that id for this account.

    User-owned, so "no such connection" and "not yours" are one condition, for the
    reason every user-scoped read gives: a caller able to tell them apart could
    enumerate another account's connections by id. The API maps it to a 404.
    """


class LLMSecretKeyUnavailable(Exception):
    """A credential was submitted, but this deployment has no master key to encrypt it.

    Distinct from a bad request: the connection is well-formed, but storing its key
    would need the `JOBSEARCH_LLM_SECRET_KEY` the deployment never set. The honest
    answer is to say the platform is not configured to hold a secret, not to store the
    key in the clear. The API maps it to a 409.
    """


class LLMConnectionInvalid(Exception):
    """The submitted fields do not form a coherent connection (a 422 at the API).

    The `LLMConnection` model owns the shape rules — a CLI carries no base URL or key,
    an API endpoint needs one — so the service does not restate them; it builds the
    model and translates its `ValidationError` into this, the one exception the API
    maps. `messages` are the model's own validator sentences (never the input that was
    rejected), so a form can show which rule failed without echoing a value back.
    """

    def __init__(self, messages: tuple[str, ...]) -> None:
        super().__init__("; ".join(messages) or "the connection is not valid")
        self.messages = messages


@dataclass(frozen=True)
class LLMConnectionDraft:
    """A connection as a settings form submits it — no id, owner, or timestamps.

    The service supplies the id and the owner and stamps the instant, so there is no
    field here to file a connection under another account or to forge its `has_api_key`
    state. `api_key` is the plaintext the user typed, held as a `SecretStr` so it does
    not print in a traceback; it is encrypted here and never stored as given.
    """

    provider_type: LLMProviderType
    display_name: str
    base_url: str | None = None
    model: str | None = None
    api_key: SecretStr | None = None
    custom_headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    is_default: bool = False
    priority: int = DEFAULT_CONNECTION_PRIORITY


@dataclass(frozen=True)
class LLMConnectionUpdate:
    """The mutable fields of a connection, each optional so an unset one is untouched.

    A field left `None` is not changed; a field given is replaced. The credential is
    the exception that needs three states, not two: `api_key` set re-encrypts a new
    one, `remove_api_key` clears the stored one, and neither leaves it as it was — so a
    user can edit a gateway's model without re-typing its key, and can rotate or drop
    the key without touching anything else.
    """

    display_name: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: SecretStr | None = None
    remove_api_key: bool = False
    custom_headers: dict[str, str] | None = None
    priority: int | None = None


class LLMConnectionService:
    """Create, edit, toggle, default, delete and probe a user's LLM connections (§4).

    Holds the repository it scopes every call through, the `SecretCipher` it encrypts a
    credential with (optional — a CLI-only deployment configures none), and the factory
    it builds a live provider from for a health probe. `http_transport` is the
    `httpx.MockTransport` seam a test injects so a healthcheck makes no real network
    call, and `resolver` is the DNS seam the SSRF check reaches through; production
    leaves both `None` and the adapter opens its own client and resolves for real.

    No clock in the constructor: the route hands `now` to each write, so a single
    request's `created_at`/`updated_at` agree — the convention every other write
    service here follows.
    """

    def __init__(self, connections: LLMConnectionRepository, *,
                 cipher: SecretCipher | None = None,
                 http_transport: httpx.AsyncBaseTransport | None = None,
                 resolver: HostResolver | None = None) -> None:
        self._connections = connections
        self._cipher = cipher
        self._factory = LLMProviderFactory(cipher=cipher,
                                           http_transport=http_transport,
                                           resolver=resolver)

    async def list_for_user(self, user_id: UserId, *,
                            limit: int = DEFAULT_LIMIT) -> tuple[LLMConnection, ...]:
        """This account's connections, in the router's own priority-then-id order."""
        return await self._connections.list_for_user(user_id, limit=limit)

    async def get(self, user_id: UserId,
                  connection_id: LLMConnectionId) -> LLMConnection:
        """One connection, or raise `LLMConnectionNotFound` (also when not this user's)."""
        found = await self._connections.get(user_id, connection_id)
        if found is None:
            raise LLMConnectionNotFound(str(connection_id))
        return found

    async def create(self, user_id: UserId, draft: LLMConnectionDraft, *,
                     now: datetime) -> LLMConnection:
        """Store a new connection for this account, encrypting any credential first.

        A random id, so this is always a fresh row — a user legitimately keeps two
        gateways of the same type (§`new_llm_connection_id`). The `LLMConnection`
        validators reject an incoherent shape (a CLI carrying a key, an API missing a
        base URL) as a 422 before anything is written. Marking it default clears the
        flag on the account's others in the same call, so the invariant holds.
        """
        encrypted, version = self._encrypt(draft.provider_type, draft.api_key)
        connection = _build(
            id=new_llm_connection_id(),
            user_id=user_id,
            provider_type=draft.provider_type,
            display_name=draft.display_name,
            base_url=draft.base_url,
            model=draft.model,
            encrypted_api_key=encrypted,
            secret_version=version,
            custom_headers=dict(draft.custom_headers),
            enabled=draft.enabled,
            is_default=draft.is_default,
            priority=draft.priority,
            created_at=now,
            updated_at=now)
        if draft.is_default:
            await self._connections.clear_default(user_id)
        return await self._connections.upsert(connection)

    async def update(self, user_id: UserId, connection_id: LLMConnectionId,
                     changes: LLMConnectionUpdate, *,
                     now: datetime) -> LLMConnection:
        """Apply a partial edit, re-encrypting or clearing the credential on request.

        Loads the connection scoped to the owner, so an id that is not this account's
        raises `LLMConnectionNotFound` rather than editing a stranger's row. An unset
        field is carried over unchanged; the credential follows the three-state rule
        `LLMConnectionUpdate` documents. The rebuilt connection is re-validated by the
        model, so an edit that would leave an API connection without a base URL is a
        422, not a broken row.
        """
        current = await self.get(user_id, connection_id)
        encrypted, version = self._resolve_secret(current, changes)
        # Rebuilt through the model, not `model_copy`: a copy does not re-run the
        # validators, so an edit that left an incoherent shape — an API connection
        # whose base URL was cleared — would slip into the store. `_build` constructs
        # a fresh `LLMConnection`, which runs the shape rules and raises
        # `LLMConnectionInvalid` (a 422) rather than persisting a broken row.
        updated = _build(
            id=current.id,
            user_id=current.user_id,
            provider_type=current.provider_type,
            display_name=_pick(changes.display_name, current.display_name),
            base_url=_pick(changes.base_url, current.base_url),
            model=_pick(changes.model, current.model),
            encrypted_api_key=encrypted,
            secret_version=version,
            custom_headers=(dict(changes.custom_headers)
                            if changes.custom_headers is not None
                            else dict(current.custom_headers)),
            enabled=current.enabled,
            is_default=current.is_default,
            priority=_pick(changes.priority, current.priority),
            created_at=current.created_at,
            updated_at=now)
        return await self._connections.upsert(updated)

    async def set_enabled(self, user_id: UserId, connection_id: LLMConnectionId,
                          enabled: bool, *, now: datetime) -> LLMConnection:
        """Turn a connection on or off without deleting it (or its stored key).

        The on/off a settings page toggles: a disabled connection keeps its row and its
        credential and is simply filtered out of the router's candidates until it is
        turned back on (`list_for_user(enabled_only=True)`).
        """
        current = await self.get(user_id, connection_id)
        return await self._connections.upsert(
            current.model_copy(update={"enabled": enabled, "updated_at": now}))

    async def set_default(self, user_id: UserId, connection_id: LLMConnectionId, *,
                          now: datetime) -> LLMConnection:
        """Make one connection the account's default, clearing the flag on the rest.

        Clears every other default first so the invariant "at most one default" holds
        after the write, then marks this one — in that order, so a crash between the
        two leaves no default rather than two.
        """
        current = await self.get(user_id, connection_id)
        await self._connections.clear_default(user_id)
        return await self._connections.upsert(
            current.model_copy(update={"is_default": True, "updated_at": now}))

    async def delete(self, user_id: UserId,
                     connection_id: LLMConnectionId) -> None:
        """Remove a connection, or raise `LLMConnectionNotFound` if it was not there.

        Scoped to the owner: a delete of an id that is not this account's removes
        nothing and raises the same not-found a never-created id does, so a caller
        cannot probe another account's connections by trying to delete them.
        """
        removed = await self._connections.delete(user_id, connection_id)
        if not removed:
            raise LLMConnectionNotFound(str(connection_id))

    async def healthcheck(self, user_id: UserId,
                          connection_id: LLMConnectionId) -> ProviderHealth:
        """Probe one connection's live provider and return its health (not stored).

        Builds the provider through the same factory the router uses — decrypting the
        credential for that instant — and asks it. The result is data, not an
        exception: a provider being down is a `ProviderHealth` with an `UNAVAILABLE`
        status, which a settings page renders, rather than an error the page must
        catch. Health is deliberately not persisted (§37).
        """
        connection = await self.get(user_id, connection_id)
        provider = self._factory.create(connection)
        return await provider.healthcheck()

    # --- credential handling ------------------------------------------------

    def _resolve_secret(self, current: LLMConnection,
                        changes: LLMConnectionUpdate) -> tuple[str | None, int | None]:
        """The (ciphertext, version) an update should carry, per its three states."""
        if changes.remove_api_key:
            return None, None
        if changes.api_key is not None:
            return self._encrypt(current.provider_type, changes.api_key)
        return current.encrypted_api_key, current.secret_version

    def _encrypt(self, provider_type: LLMProviderType,
                 api_key: SecretStr | None) -> tuple[str | None, int | None]:
        """Encrypt a submitted credential, or return the no-credential pair.

        A CLI connection is left keyless here without complaint — the `LLMConnection`
        validator is what refuses a key on one, with a message a form can show — so a
        stray key on a CLI draft is ignored rather than encrypted and then rejected.
        An API credential with no configured cipher is an `LLMSecretKeyUnavailable`:
        the deployment cannot hold a secret, and storing it in the clear is not an
        option the service will take.
        """
        if api_key is None or provider_type.is_cli:
            return None, None
        if self._cipher is None:
            raise LLMSecretKeyUnavailable(
                "this deployment has no master key configured, so an API credential "
                "cannot be stored")
        return self._cipher.encrypt(api_key), self._cipher.version or CURRENT_SECRET_VERSION


def _pick(new: object | None, current: object) -> object:
    """The new value when an update supplied one, else the current — for a plain field."""
    return current if new is None else new


def _build(**fields: object) -> LLMConnection:
    """Construct an `LLMConnection`, turning its refusal into `LLMConnectionInvalid`.

    The model is the single owner of the shape rules; this only translates the
    `ValidationError` it raises into the exception the API maps to a 422. The rebuilt
    messages are the validators' own sentences, never the rejected input — a Pydantic
    `ValidationError` can quote the offending value in its representation, so it is not
    forwarded (the same reason `LLMDocumentGenerator` does not forward one).
    """
    try:
        return LLMConnection(**fields)  # type: ignore[arg-type]
    except ValidationError as exc:
        messages = tuple(str(error.get("msg", "invalid value"))
                         for error in exc.errors())
        raise LLMConnectionInvalid(messages) from exc
