"""`/api/v2/settings/llm`: a user's LLM connections, and nothing that runs one.

The settings surface for the provider-neutral platform. It configures *where* a
prompt could go — the connections a user has stored, each a provider type, an
endpoint and a model, with a credential this API accepts but never returns — and it
probes whether one is reachable. It does not run a generation: there is no
`POST /llm/complete`, no raw prompt endpoint (§9). A task that needs a model routes
through its own service (documents, matching, chat), which builds a request from a
versioned prompt; this router only decides what providers that router may choose from.

One authorization model, the same the rest of `/api/v2` follows: the owner comes
from the session, never the path or the body (docs/ENGINEERING_STANDARDS.md
§Security). A connection id that belongs to another account reads as absent — the
service raises the same not-found for "no such connection" and "not yours", so a
caller cannot enumerate another account's connections by id.

The credential is write-only end to end. `POST` and `PATCH` accept an `api_key`;
every response is an `LLMConnectionResponse`, which has no field for the value or its
ciphertext, only `has_api_key` (§13). Rotating, clearing and leaving a key untouched
are three distinct edits `PATCH` expresses, so a user changes a gateway's model
without re-typing its key.

Health is a live probe returned as data, not a stored fact (§37): `POST
/{id}/healthcheck` builds the connection's provider and asks it, and a provider that
is down answers with an `UNAVAILABLE` status the settings page renders, not an error
it must catch.
"""
from fastapi import APIRouter, status

from backend.app.api.dependencies import CurrentSession, LLMConnections, Now
from backend.app.api.schemas import (
    CreateLLMConnectionRequest,
    LLMConnectionHealthResponse,
    LLMConnectionListResponse,
    LLMConnectionResponse,
    SetLLMConnectionEnabledRequest,
    UpdateLLMConnectionRequest,
)
from backend.app.domain.identifiers import LLMConnectionId
from backend.app.services.llm_connections import (
    LLMConnectionDraft,
    LLMConnectionUpdate,
)

router = APIRouter(prefix="/settings/llm", tags=["v2-llm-settings"])


@router.get("/connections", response_model=LLMConnectionListResponse)
async def list_connections(current: CurrentSession,
                           service: LLMConnections) -> LLMConnectionListResponse:
    """This account's connections, in the router's priority-then-id order.

    Every credential is reduced to `has_api_key`; the values never leave the service.
    """
    connections = await service.list_for_user(current.user.id)
    return LLMConnectionListResponse.of(connections)


@router.post("/connections", response_model=LLMConnectionResponse,
             status_code=status.HTTP_201_CREATED)
async def create_connection(body: CreateLLMConnectionRequest, current: CurrentSession,
                            service: LLMConnections,
                            instant: Now) -> LLMConnectionResponse:
    """Store a new connection for this account, encrypting any credential first.

    201, because it creates a resource: the response carries the id the connection was
    filed under, which a later edit, probe or default targets. 422 when the shape is
    incoherent (a CLI carrying a key, an API missing a base URL) — the `LLMConnection`
    model decides that once, for every writer. 409 (`llm_secret_key_unavailable`) when
    a credential is submitted but the deployment configured no master key to encrypt
    it: the honest answer, rather than storing the key in the clear.
    """
    connection = await service.create(
        current.user.id,
        LLMConnectionDraft(
            provider_type=body.provider_type, display_name=body.display_name,
            base_url=body.base_url, model=body.model, api_key=body.api_key,
            custom_headers=dict(body.custom_headers), enabled=body.enabled,
            is_default=body.is_default, priority=body.priority),
        now=instant)
    return LLMConnectionResponse.of(connection)


@router.get("/connections/{connection_id}", response_model=LLMConnectionResponse)
async def read_connection(connection_id: LLMConnectionId, current: CurrentSession,
                          service: LLMConnections) -> LLMConnectionResponse:
    """One connection, credential reduced to `has_api_key`.

    404 for "no such connection" and "not yours" alike — the service raises one error
    for both, so a caller cannot enumerate another account's connections by id.
    """
    connection = await service.get(current.user.id, connection_id)
    return LLMConnectionResponse.of(connection)


@router.patch("/connections/{connection_id}", response_model=LLMConnectionResponse)
async def update_connection(connection_id: LLMConnectionId,
                            body: UpdateLLMConnectionRequest, current: CurrentSession,
                            service: LLMConnections,
                            instant: Now) -> LLMConnectionResponse:
    """Apply a partial edit, re-encrypting or clearing the credential on request.

    A `PATCH`, not a `PUT`: an unset field is left as stored, and the credential has
    three states — `api_key` rotates it, `remove_api_key` clears it, neither leaves it.
    Sending both is a 422, refused before the service is called. 404 when the id is not
    this account's; 422 when the edit would leave an incoherent shape; 409 when a new
    credential is given but no master key is configured.
    """
    connection = await service.update(
        current.user.id, connection_id,
        LLMConnectionUpdate(
            display_name=body.display_name, base_url=body.base_url, model=body.model,
            api_key=body.api_key, remove_api_key=body.remove_api_key,
            custom_headers=body.custom_headers, priority=body.priority),
        now=instant)
    return LLMConnectionResponse.of(connection)


@router.put("/connections/{connection_id}/enabled",
            response_model=LLMConnectionResponse)
async def set_connection_enabled(connection_id: LLMConnectionId,
                                 body: SetLLMConnectionEnabledRequest,
                                 current: CurrentSession, service: LLMConnections,
                                 instant: Now) -> LLMConnectionResponse:
    """Turn a connection on or off without deleting it or its stored key.

    A disabled connection keeps its row and its credential and is simply filtered out
    of the router's candidates until it is turned back on. 404 when not this account's.
    """
    connection = await service.set_enabled(
        current.user.id, connection_id, body.enabled, now=instant)
    return LLMConnectionResponse.of(connection)


@router.put("/connections/{connection_id}/default",
            response_model=LLMConnectionResponse)
async def set_connection_default(connection_id: LLMConnectionId,
                                 current: CurrentSession, service: LLMConnections,
                                 instant: Now) -> LLMConnectionResponse:
    """Make one connection the account's default, clearing the flag on the rest.

    The invariant "at most one default" holds after the write: the service clears every
    other default first, then marks this one. 404 when not this account's.
    """
    connection = await service.set_default(current.user.id, connection_id, now=instant)
    return LLMConnectionResponse.of(connection)


@router.delete("/connections/{connection_id}",
               status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(connection_id: LLMConnectionId, current: CurrentSession,
                            service: LLMConnections) -> None:
    """Delete one connection, and its stored credential with it.

    Not idempotent, on purpose: a second `DELETE` answers 404 rather than 204, so a
    client working from a stale list is told rather than misled. Scoped to the owner —
    a delete of an id that is not this account's removes nothing and 404s, the same as
    a never-created id, so a caller cannot probe another account by trying to delete.
    """
    await service.delete(current.user.id, connection_id)


@router.post("/connections/{connection_id}/healthcheck",
             response_model=LLMConnectionHealthResponse)
async def healthcheck_connection(connection_id: LLMConnectionId,
                                 current: CurrentSession,
                                 service: LLMConnections
                                 ) -> LLMConnectionHealthResponse:
    """Probe one connection's live provider and return its health (not stored).

    A `POST` because it does work — it builds the provider and reaches out — even
    though it stores nothing. The result is data: a provider that is down is an
    `UNAVAILABLE` status the settings page renders, and its `detail` is a secret-free
    sentence the platform composed, never a raw provider message. 404 when the id is
    not this account's.
    """
    health = await service.healthcheck(current.user.id, connection_id)
    return LLMConnectionHealthResponse.of(health)
