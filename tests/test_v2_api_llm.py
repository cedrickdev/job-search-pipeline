# tests/test_v2_api_llm.py
"""`/api/v2/settings/llm` over HTTP: a user's LLM connections, and never a key.

The settings surface for the provider-neutral platform, driven the way a browser
drives it. Three properties matter enough to pin here, because the route layer and
`create_app`'s wiring decide them and a service test would assert none of them:

- **the credential is write-only end to end.** A `POST` accepts an `api_key` and a
  `has_api_key` comes back `true`, but no response — create, read, list, update —
  ever carries the value or its ciphertext. The store holds a real Fernet ciphertext
  (the harness wires a real cipher), so this is the whole round trip, not a stub.
- **a connection is authorization-scoped.** A second account naming the first's
  connection id gets the same 404 as an id that never existed, and the refusal leaves
  the owner's row untouched — both accounts share one store here, so a scoping slip
  would surface as a leak rather than a 404.
- **the shape rules are the model's, enforced once.** An API connection with no base
  URL is a 422; a CLI connection silently stores no key even if one is sent, because
  §1 forbids the platform ever holding a CLI credential.

Health is a live probe returned as data: the harness answers `GET /models` over a
`MockTransport`, so a healthy connection reports `HEALTHY` without a socket, and a
local connection pointed at a public host surfaces the platform's own
`PROVIDER_MISCONFIGURED` as a mapped status rather than a leaked provider message.
"""
from datetime import timedelta
from uuid import UUID

import httpx
import pytest

from backend.app.api.dependencies import llm_connection_service
from backend.app.services.llm_connections import LLMConnectionService
from tests.v2_api import OTHER_EMAIL, api_harness

# One valid body per provider shape the surface accepts. A local OpenAI-compatible
# endpoint needs a loopback base URL; a Claude Code CLI connection needs neither a URL
# nor a key. Invented values only — no real endpoint, no real credential.
LOCAL_BODY = {
    "provider_type": "LOCAL_OPENAI_COMPATIBLE",
    "display_name": "Local Ollama",
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "llama3.1",
}
API_BODY = {
    "provider_type": "OPENAI_COMPATIBLE",
    "display_name": "Corp gateway",
    "base_url": "https://gateway.example.invalid/v1",
    "model": "gateway-model",
    "api_key": "sk-not-a-real-key-000000",
}
CLI_BODY = {
    "provider_type": "CLAUDE_CODE",
    "display_name": "Claude Code",
}

PREFIX = "/settings/llm/connections"


@pytest.mark.asyncio
async def test_a_connection_is_created_read_listed_and_deleted_once(tmp_path):
    """The lifecycle verbs, and the second `DELETE` answering 404 on purpose.

    A client that deletes the same connection twice is working from a stale list, and
    404 tells it so rather than pretending the second call removed something.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        created = await api.write("POST", PREFIX, json=LOCAL_BODY)
        assert created.status_code == 201, created.text
        connection_id = created.json()["id"]
        assert created.json()["display_name"] == "Local Ollama"
        assert created.json()["provider_type"] == "LOCAL_OPENAI_COMPATIBLE"

        listed = await api.read(PREFIX)
        assert listed.status_code == 200
        assert [c["id"] for c in listed.json()["connections"]] == [connection_id]

        one = await api.read(f"{PREFIX}/{connection_id}")
        assert one.status_code == 200
        assert one.json() == created.json()

        gone = await api.write("DELETE", f"{PREFIX}/{connection_id}")
        assert gone.status_code == 204
        again = await api.write("DELETE", f"{PREFIX}/{connection_id}")
        assert again.status_code == 404
        assert again.json()["error"] == "llm_connection_not_found"


@pytest.mark.asyncio
async def test_the_api_key_is_stored_but_never_returned(tmp_path):
    """A submitted credential comes back only as `has_api_key`, never its value."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        created = await api.write("POST", PREFIX, json=API_BODY)
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["has_api_key"] is True
        # The value and the field it was sent under are both absent from the response.
        assert "api_key" not in body
        assert API_BODY["api_key"] not in created.text

        # And it is genuinely encrypted in the store, not held in the clear.
        connection_id = body["id"]
        stored = next(iter(api.llm_connections.connections.values()))
        assert stored.encrypted_api_key is not None
        assert API_BODY["api_key"] not in (stored.encrypted_api_key or "")
        assert stored.secret_version is not None

        fetched = await api.read(f"{PREFIX}/{connection_id}")
        assert API_BODY["api_key"] not in fetched.text


@pytest.mark.asyncio
async def test_the_credential_rotates_clears_and_stays_across_edits(tmp_path):
    """`PATCH` expresses three credential states; `remove_api_key` with a key is 422."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        created = await api.write("POST", PREFIX, json=API_BODY)
        connection_id = created.json()["id"]
        first_cipher = next(
            iter(api.llm_connections.connections.values())).encrypted_api_key

        # Editing the model without a key leaves the stored credential untouched.
        api.clock.advance(timedelta(minutes=1))
        edited = await api.write("PATCH", f"{PREFIX}/{connection_id}",
                                 json={"model": "gateway-model-v2"})
        assert edited.status_code == 200
        assert edited.json()["model"] == "gateway-model-v2"
        assert edited.json()["has_api_key"] is True
        unchanged = next(
            iter(api.llm_connections.connections.values())).encrypted_api_key
        assert unchanged == first_cipher

        # A new key re-encrypts: the ciphertext changes, the value never appears.
        rotated = await api.write("PATCH", f"{PREFIX}/{connection_id}",
                                  json={"api_key": "sk-rotated-key-11111111"})
        assert rotated.status_code == 200
        assert rotated.json()["has_api_key"] is True
        assert "sk-rotated-key-11111111" not in rotated.text

        # Clearing drops it to keyless.
        cleared = await api.write("PATCH", f"{PREFIX}/{connection_id}",
                                  json={"remove_api_key": True})
        assert cleared.status_code == 200
        assert cleared.json()["has_api_key"] is False

        # Asking to both rotate and clear is contradictory — refused as a 422.
        conflict = await api.write(
            "PATCH", f"{PREFIX}/{connection_id}",
            json={"api_key": "sk-both-22222222", "remove_api_key": True})
        assert conflict.status_code == 422


@pytest.mark.asyncio
async def test_a_cli_connection_stores_no_key_and_an_api_needs_a_base_url(tmp_path):
    """The shape rules are the model's: a CLI holds no key, an API needs a URL."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()

        # A CLI connection with a stray key: the key is dropped, not stored, and the
        # connection is created keyless rather than refused.
        cli = await api.write("POST", PREFIX,
                              json={**CLI_BODY, "api_key": "sk-ignored-33333333"})
        assert cli.status_code == 201, cli.text
        assert cli.json()["has_api_key"] is False
        assert cli.json()["base_url"] is None

        # An OpenAI-compatible connection with no base URL cannot be reached and is a
        # 422 — the hostname is never assumed (§5).
        no_url = await api.write("POST", PREFIX,
                                 json={"provider_type": "OPENAI_COMPATIBLE",
                                       "display_name": "Bad", "model": "x"})
        assert no_url.status_code == 422


@pytest.mark.asyncio
async def test_exactly_one_connection_is_default(tmp_path):
    """Marking a second default clears the first, so the invariant holds after."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        first = (await api.write("POST", PREFIX,
                                 json={**LOCAL_BODY, "is_default": True})).json()
        second = (await api.write("POST", PREFIX, json=API_BODY)).json()
        assert first["is_default"] is True
        assert second["is_default"] is False

        promoted = await api.write("PUT", f"{PREFIX}/{second['id']}/default")
        assert promoted.status_code == 200
        assert promoted.json()["is_default"] is True

        # The first is no longer default — read it back to confirm the clear.
        refetched = await api.read(f"{PREFIX}/{first['id']}")
        assert refetched.json()["is_default"] is False


@pytest.mark.asyncio
async def test_a_connection_can_be_disabled_and_re_enabled(tmp_path):
    """The on/off keeps the row and its key; it only leaves the router's candidates."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        created = (await api.write("POST", PREFIX, json=API_BODY)).json()

        disabled = await api.write("PUT", f"{PREFIX}/{created['id']}/enabled",
                                   json={"enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["has_api_key"] is True  # the key is kept

        enabled = await api.write("PUT", f"{PREFIX}/{created['id']}/enabled",
                                  json={"enabled": True})
        assert enabled.json()["enabled"] is True


@pytest.mark.asyncio
async def test_another_account_cannot_see_or_touch_a_connection(tmp_path):
    """A stranger's id 404s the same as a missing one, and the owner's row is intact."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        created = (await api.write("POST", PREFIX, json=API_BODY)).json()
        connection_id = created["id"]

        # A second account, its own signed-in session in the same store.
        await api.sign_in(email=OTHER_EMAIL)

        assert (await api.read(f"{PREFIX}/{connection_id}")).status_code == 404
        assert (await api.write("PATCH", f"{PREFIX}/{connection_id}",
                                json={"model": "hijacked"})).status_code == 404
        assert (await api.write("DELETE", f"{PREFIX}/{connection_id}")).status_code == 404
        # The second account's own list is empty — the first's connection never leaked.
        assert (await api.read(PREFIX)).json()["connections"] == []

        # And the owner's row is exactly as it was.
        stored = api.llm_connections.connections[UUID(connection_id)]
        assert stored.model == "gateway-model"


@pytest.mark.asyncio
async def test_a_healthy_connection_probes_healthy(tmp_path):
    """A `POST /healthcheck` builds the provider and probes it — data, not an error."""
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        created = (await api.write("POST", PREFIX, json=LOCAL_BODY)).json()

        probed = await api.write("POST", f"{PREFIX}/{created['id']}/healthcheck")
        assert probed.status_code == 200
        assert probed.json()["status"] == "HEALTHY"


@pytest.mark.asyncio
async def test_a_misconfigured_local_connection_surfaces_a_typed_llm_error(tmp_path):
    """A local connection pointed at a public host is `PROVIDER_MISCONFIGURED` → 409.

    The connection stores fine — the model only requires a base URL — but building its
    provider validates the address, and a "local" provider must be loopback. The
    failure is the platform's own typed, secret-free error, mapped to a status a client
    can act on, not a leaked provider message.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        created = await api.write(
            "POST", PREFIX,
            json={**LOCAL_BODY, "base_url": "http://10.0.0.5:11434/v1"})
        assert created.status_code == 201, created.text
        connection_id = created.json()["id"]

        probed = await api.write("POST", f"{PREFIX}/{connection_id}/healthcheck")
        assert probed.status_code == 409
        assert probed.json()["error"] == "provider_misconfigured"


@pytest.mark.asyncio
async def test_a_credential_without_a_master_key_is_a_conflict(tmp_path):
    """Submitting a key when the deployment configured none is a 409, not a 422.

    The request is well-formed; storing its credential would need a master key the
    deployment never set, and the honest answer is to say so rather than store the key
    in the clear. Overrides the service with a cipher-less one for this deployment.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        api.app.dependency_overrides[llm_connection_service] = (
            lambda: LLMConnectionService(api.llm_connections, cipher=None))

        refused = await api.write("POST", PREFIX, json=API_BODY)
        assert refused.status_code == 409
        assert refused.json()["error"] == "llm_secret_key_unavailable"

        # A CLI connection still works with no master key — it stores no credential.
        cli = await api.write("POST", PREFIX, json=CLI_BODY)
        assert cli.status_code == 201


@pytest.mark.asyncio
async def test_the_settings_surface_needs_a_signed_in_session(tmp_path):
    """Every connection route is behind the session gate; anonymous reads 401."""
    async with api_harness(tmp_path) as api:
        anonymous = await api.client.get(api.url(PREFIX))
        assert anonymous.status_code == 401


def test_the_mock_transport_stays_off_the_network():
    """A guard on the fixture itself: the healthcheck seam is a transport, no socket."""
    from tests.v2_api import _healthcheck_ok
    response = _healthcheck_ok(httpx.Request("GET", "http://127.0.0.1/v1/models"))
    assert response.status_code == 200
