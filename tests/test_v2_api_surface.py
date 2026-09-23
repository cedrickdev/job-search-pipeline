# tests/test_v2_api_surface.py
"""What `/api/v2` publishes, and what V1 still publishes beside it.

Three of the nine properties Phase 4 has to demonstrate are here: that an
unauthenticated caller is refused by every V2 operation except register and login,
that no response model can carry a credential, and that V1 is behaviourally
unchanged. Two more are here because they are surface facts rather than flow facts:
the exact route inventory, and that nothing answering a safe method mutates state.

The inventory tests read `app.openapi()` rather than `app.routes`: FastAPI wraps the
routers it includes, so the router objects are not the list a client sees, and the
document is exactly what `scripts/dump_openapi.py` hands the frontend's type
generator. A route added without a test fails the first assertion in this module,
which is the point of pinning a count that a normal change has no reason to touch.

The counts moved once at Phase 6 — the three company operations — again at
Phase 7, which adds the three geo reads: postings near a place, employers near a
place, and one saved search run as a geo query, again at Phase 9, which adds the
three assessment routes: evaluate a pair, read one pair, list a user's pairs,
again at Phase 10, which adds eight: the two evidence writes and the evidence read
under `/me`, the two document generators keyed by posting, and the list, read and
download of a generated document, and again at Phase 11, which adds eight for the
LLM connection settings surface: list and create, read, edit (`PATCH`), enable and
default, delete, and a healthcheck probe. All of them are held to the same rules the
Phase 4 twelve are — under the prefix, authenticated, safe where they read, and
carrying no credential field. Phase 11 is where the last rule earns its keep: the
create and edit bodies accept an `api_key`, and no response schema may echo it.

Phase 13 adds eight for the career-chat control plane: four reads (the conversation
list, one thread, its messages, its proposals) and four writes (open a thread, the one
streaming-turn `POST .../messages`, and confirm/dismiss a proposal). The chat obeys the
same last rule — a `ChatAction` and every chat response is secret-free by construction —
so nothing here can echo a credential.
"""
from copy import deepcopy
from datetime import timedelta

import pytest

from backend.app.api import API_V2_PREFIX
from backend.app.documents import LocalDocumentArtifactStore
from backend.app.domain.application import Application, build_idempotency_key
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.identifiers import (
    ApplicationId,
    CandidateDocumentId,
    CompanyId,
    ConversationId,
    LLMConnectionId,
    OpportunityId,
    SearchProfileId,
    default_candidate_profile_id,
    document_version_id,
    eligibility_result_id,
)
from backend.app.services.authentication import SESSION_TOUCH_INTERVAL
from tests.v2_api import (
    PLACEHOLDER_ID,
    api_harness,
    concrete,
    operations,
    schema_property_names,
)
from tests.v2_builders import (
    a_company,
    a_conversation,
    a_decision,
    a_rendered_document,
    a_search_profile,
    an_eligibility_result,
    an_llm_connection,
    an_opportunity,
)

# The whole V2 surface as of Phase 13, spelled out. Written as a literal on purpose:
# a test that derived it from the application would agree with any change.
V2_OPERATIONS = (
    ("DELETE", "/api/v2/me/search-profiles/{search_profile_id}"),
    ("DELETE", "/api/v2/settings/llm/connections/{connection_id}"),
    ("GET", "/api/v2/applications"),
    ("GET", "/api/v2/applications/{application_id}"),
    ("GET", "/api/v2/applications/{application_id}/events"),
    ("GET", "/api/v2/auth/session"),
    ("GET", "/api/v2/chat/conversations"),
    ("GET", "/api/v2/chat/conversations/{conversation_id}"),
    ("GET", "/api/v2/chat/conversations/{conversation_id}/messages"),
    ("GET", "/api/v2/chat/conversations/{conversation_id}/proposals"),
    ("GET", "/api/v2/companies"),
    ("GET", "/api/v2/companies/{company_id}"),
    ("GET", "/api/v2/documents"),
    ("GET", "/api/v2/documents/{document_id}"),
    ("GET", "/api/v2/documents/{document_id}/download"),
    ("GET", "/api/v2/geo/companies"),
    ("GET", "/api/v2/geo/opportunities"),
    ("GET", "/api/v2/matches"),
    ("GET", "/api/v2/me/evidence"),
    ("GET", "/api/v2/me/profile"),
    ("GET", "/api/v2/me/search-profiles"),
    ("GET", "/api/v2/me/search-profiles/{search_profile_id}/opportunities"),
    ("GET", "/api/v2/onboarding"),
    ("GET", "/api/v2/opportunities/{opportunity_id}/match"),
    ("GET", "/api/v2/settings/llm/connections"),
    ("GET", "/api/v2/settings/llm/connections/{connection_id}"),
    ("PATCH", "/api/v2/settings/llm/connections/{connection_id}"),
    ("POST", "/api/v2/applications"),
    ("POST", "/api/v2/applications/{application_id}/approve"),
    ("POST", "/api/v2/applications/{application_id}/cancel"),
    ("POST", "/api/v2/applications/{application_id}/prepare"),
    ("POST", "/api/v2/applications/{application_id}/submit"),
    ("POST", "/api/v2/auth/login"),
    ("POST", "/api/v2/auth/logout"),
    ("POST", "/api/v2/auth/register"),
    ("POST", "/api/v2/chat/conversations"),
    ("POST", "/api/v2/chat/conversations/{conversation_id}/messages"),
    ("POST", "/api/v2/chat/proposals/{proposal_id}/confirm"),
    ("POST", "/api/v2/chat/proposals/{proposal_id}/dismiss"),
    ("POST", "/api/v2/company-discovery/run"),
    ("POST", "/api/v2/matches/evaluate"),
    ("POST", "/api/v2/me/claims"),
    ("POST", "/api/v2/me/evidence"),
    ("POST", "/api/v2/me/search-profiles"),
    ("POST", "/api/v2/onboarding/complete"),
    ("POST", "/api/v2/opportunities/{opportunity_id}/cover-letter"),
    ("POST", "/api/v2/opportunities/{opportunity_id}/resume"),
    ("POST", "/api/v2/settings/llm/connections"),
    ("POST", "/api/v2/settings/llm/connections/{connection_id}/healthcheck"),
    ("PUT", "/api/v2/me/profile"),
    ("PUT", "/api/v2/me/search-profiles/{search_profile_id}"),
    ("PUT", "/api/v2/settings/llm/connections/{connection_id}/default"),
    ("PUT", "/api/v2/settings/llm/connections/{connection_id}/enabled"),
)

# The two operations a caller reaches without a session, because their purpose is to
# obtain one. Everything else in `V2_OPERATIONS` must answer 401.
PUBLIC_OPERATIONS = frozenset({
    ("POST", "/api/v2/auth/login"),
    ("POST", "/api/v2/auth/register"),
})

# Substrings that must not name a field in any V2 response. `hash` and `digest`
# cover the stored forms, `token` and `csrf` the live ones.
FORBIDDEN_IN_RESPONSES = ("password", "token", "digest", "hash", "secret",
                          "credential", "csrf")

# V1's published surface, as counts. 29 operations over 28 paths — `/api/settings`
# is the one path with two methods. Counts rather than a list of 28 strings: what
# Phase 4 has to prove is that adding the V2 router removed nothing, and a count
# says that without duplicating a V1 inventory that V1's own tests already cover.
V1_OPERATIONS = 29
V1_PATHS = 28


def _get_with_scope(path: str) -> str:
    """A callable URL for one GET in the twice-over sweep.

    The two open geo lists refuse a query with no scope — a full-table scan is not a
    search (§14) — so the sweep gives them the smallest valid one. Every other GET,
    including the saved-search geo read whose scope comes from the profile, is
    callable exactly as its template concretes.
    """
    if path.endswith(("/geo/opportunities", "/geo/companies")):
        return f"{path}?country=CH"
    return path


@pytest.mark.asyncio
async def test_the_v2_surface_is_exactly_the_operations_phases_4_6_7_9_and_10_define(
        tmp_path):
    """The inventory, and every path scoped under the one prefix.

    The second assertion is the one that matters for the frontend: a V2 route
    mounted outside `/api/v2` would not be proxied by Nuxt, and a V1 path that
    slipped into the prefix would inherit the CSRF dependency and break V1 clients.
    """
    async with api_harness(tmp_path) as api:
        published = operations(api.app, under=API_V2_PREFIX)

        assert published == V2_OPERATIONS
        assert all(path.startswith(f"{API_V2_PREFIX}/") for _, path in published)
        assert len({path for _, path in published}) == 43


@pytest.mark.asyncio
async def test_a_safe_method_is_only_published_where_nothing_is_written(tmp_path):
    """The inventory half of "no GET mutates state".

    Twenty-four `GET`s, all of them reports. `POST /onboarding/complete` exists precisely
    so that the screen displaying progress does not have to be the thing that records
    it, and `POST /company-discovery/run` is the same split for the directory: reading
    it is safe, filling it is a write an operator triggers. The three Phase 7 reads
    join the reports — a map is a view, and viewing it writes nothing — and so do the
    two Phase 9 assessment reads. Phase 10 adds four more reads: the evidence store
    (`GET /me/evidence`), the document list and one document (`GET /documents`,
    `GET /documents/{id}`), and the PDF download — a stream is still a read, and the
    two document generators (`POST .../resume`, `POST .../cover-letter`) are the
    writes that produce what it streams. Phase 13 adds four chat reads — the
    conversation list, one thread, its messages and its proposals — all views; the
    streaming turn (`POST .../messages`) and the confirm/dismiss are its writes.
    """
    async with api_harness(tmp_path) as api:
        published = operations(api.app, under=API_V2_PREFIX)

        assert {path for method, path in published if method == "GET"} == {
            "/api/v2/applications",
            "/api/v2/applications/{application_id}",
            "/api/v2/applications/{application_id}/events",
            "/api/v2/auth/session",
            "/api/v2/chat/conversations",
            "/api/v2/chat/conversations/{conversation_id}",
            "/api/v2/chat/conversations/{conversation_id}/messages",
            "/api/v2/chat/conversations/{conversation_id}/proposals",
            "/api/v2/companies",
            "/api/v2/companies/{company_id}",
            "/api/v2/documents",
            "/api/v2/documents/{document_id}",
            "/api/v2/documents/{document_id}/download",
            "/api/v2/geo/companies",
            "/api/v2/geo/opportunities",
            "/api/v2/matches",
            "/api/v2/me/evidence",
            "/api/v2/me/profile",
            "/api/v2/me/search-profiles",
            "/api/v2/me/search-profiles/{search_profile_id}/opportunities",
            "/api/v2/onboarding",
            "/api/v2/opportunities/{opportunity_id}/match",
            "/api/v2/settings/llm/connections",
            "/api/v2/settings/llm/connections/{connection_id}"}
        # `HEAD`/`OPTIONS`/`TRACE` are never published. `PATCH` now is — the one
        # partial edit the surface has, on an LLM connection — so it is not forbidden,
        # only absent from the safe-method (`GET`) set asserted above.
        assert not [method for method, _ in published
                    if method in {"HEAD", "OPTIONS", "TRACE"}]


@pytest.mark.asyncio
async def test_calling_every_get_twice_leaves_every_store_identical(tmp_path):
    """The behavioural half, asserted against the stores rather than the bodies.

    A `GET` that wrote something would be a request an attacker's page could make
    cross-site with the browser's cookie attached — `SameSite=Lax` sends the session
    cookie on a top-level navigation, and no CSRF header is required of a safe
    method. So the rule has to hold in the handlers, not only in the route table.

    The company stored under `PLACEHOLDER_ID` is what makes the sweep meaningful for
    the detail route: without it that `GET` would 404 before reaching the service, and
    a handler that wrote on the way to a 200 would never be exercised. The saved search
    stored under the same id does the same job for the geo read on
    `/me/search-profiles/{id}/opportunities` — it has to resolve to a profile this
    account owns, or the sweep would exercise the 404 rather than the 200 path.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        await api.finish_onboarding()
        user_id = next(iter(api.users.users))
        await api.companies.upsert(a_company(id=CompanyId(PLACEHOLDER_ID),
                                             locations=()))
        await api.searches.upsert(a_search_profile(
            id=SearchProfileId(PLACEHOLDER_ID), user_id=user_id))
        # `GET /opportunities/{id}/match` reads a stored verdict, so the sweep only
        # exercises its 200 path when the pair has one: a posting under the
        # placeholder id and an eligibility keyed on this account's default profile
        # and that posting. Both are seeded so the read is real, not a 404.
        posting_id = OpportunityId(PLACEHOLDER_ID)
        profile_id = default_candidate_profile_id(user_id)
        await api.postings.upsert(an_opportunity(id=posting_id))
        await api.eligibilities.upsert(an_eligibility_result(
            id=eligibility_result_id(profile_id, posting_id),
            user_id=user_id, candidate_profile_id=profile_id,
            opportunity_id=posting_id))
        # `GET /documents/{id}` and its `/download` read a stored document, so the
        # sweep only exercises their 200 path when one exists under the placeholder
        # id. A RENDERED version is seeded, and its PDF is written under the same
        # storage key the harness's artifact store uses, so the download streams
        # real bytes rather than raising `ArtifactNotFound` on the way to a 500.
        document_id = CandidateDocumentId(PLACEHOLDER_ID)
        store = LocalDocumentArtifactStore(tmp_path / "document_artifacts")
        storage_key = store.key_for(document_id,
                                    document_version_id(document_id, 1))
        store.put(storage_key, b"%PDF-1.7\n%stub\n")
        await api.documents.upsert(a_rendered_document(
            id=document_id, user_id=user_id, candidate_profile_id=profile_id,
            opportunity_id=posting_id, storage_key=storage_key))
        # `GET /settings/llm/connections/{id}` reads a stored connection, so the sweep
        # exercises its 200 path only when one exists under the placeholder id and this
        # account. The list read beside it needs no seed — an empty list is a 200.
        await api.llm_connections.upsert(an_llm_connection(
            id=LLMConnectionId(PLACEHOLDER_ID), user_id=user_id))
        # `GET /applications/{id}` and its `/events` read a stored application, so the
        # sweep exercises their 200 path only when one exists under the placeholder id
        # and this account. The id is the placeholder rather than the derived one — the
        # read is by id — and the idempotency key is the real one for the pair, which is
        # all the aggregate's validator requires.
        decision = await api.application_decisions.upsert(a_decision(
            candidate_profile_id=profile_id, opportunity_id=posting_id,
            user_id=user_id))
        key = build_idempotency_key(candidate_profile_id=profile_id,
                                    channel=ApplicationChannel.BROWSER,
                                    opportunity_id=posting_id)
        await api.applications.upsert(Application(
            id=ApplicationId(PLACEHOLDER_ID), user_id=user_id,
            candidate_profile_id=profile_id, decision_id=decision.id,
            channel=ApplicationChannel.BROWSER, idempotency_key=key,
            opportunity_id=posting_id, created_at=api.clock.instant,
            updated_at=api.clock.instant))
        # The three chat reads keyed by conversation — detail, messages, proposals —
        # 404 before the service unless a thread exists under the placeholder id for
        # this account, so one is seeded. The conversation *list* read needs no seed;
        # an empty list is a 200.
        await api.conversations.upsert(a_conversation(
            id=ConversationId(PLACEHOLDER_ID), user_id=user_id))
        reads = [_get_with_scope(concrete(path)) for method, path in operations(
            api.app, under=API_V2_PREFIX) if method == "GET"]
        before = deepcopy((api.users.users, api.sessions.sessions,
                           api.profiles.profiles, api.searches.searches,
                           api.companies.companies, api.career_sites.sites,
                           api.discoveries.records, api.postings.opportunities,
                           api.matches.evaluations, api.eligibilities.results,
                           api.documents.documents, api.llm_connections.connections,
                           api.conversations.conversations, api.chat_messages.messages,
                           api.chat_proposals.proposals))

        for path in reads:
            for _ in range(2):
                response = await api.client.get(path)
                assert response.status_code == 200, path

        assert (api.users.users, api.sessions.sessions, api.profiles.profiles,
                api.searches.searches, api.companies.companies,
                api.career_sites.sites, api.discoveries.records,
                api.postings.opportunities, api.matches.evaluations,
                api.eligibilities.results, api.documents.documents,
                api.llm_connections.connections, api.conversations.conversations,
                api.chat_messages.messages, api.chat_proposals.proposals) == before


@pytest.mark.asyncio
async def test_the_one_write_a_get_performs_is_last_seen_at_and_it_cannot_extend(
        tmp_path):
    """`last_seen_at` moves; `expires_at` does not, which is what makes it safe.

    Recording that a session was used is not a state change in the sense the CSRF
    rules mean: it is idempotent, it concerns the caller's own row, and because the
    lifetime is absolute it cannot lengthen a stolen cookie's usefulness. The test
    exists to hold that last part — a sliding expiry implemented here would look like
    a harmless bookkeeping change.
    """
    async with api_harness(tmp_path) as api:
        await api.sign_in()
        issued = next(iter(api.sessions.sessions.values()))
        api.clock.advance(SESSION_TOUCH_INTERVAL + timedelta(seconds=1))

        assert (await api.read("/auth/session")).status_code == 200

        touched = next(iter(api.sessions.sessions.values()))
        assert touched.last_seen_at == api.clock.instant
        assert touched.last_seen_at > issued.last_seen_at
        assert touched.expires_at == issued.expires_at
        assert touched == issued.model_copy(
            update={"last_seen_at": touched.last_seen_at})


@pytest.mark.asyncio
async def test_every_operation_but_register_and_login_refuses_an_anonymous_caller(
        tmp_path):
    """401 from all fifty-one, with no body sent and nothing created.

    No payload is needed because FastAPI resolves the session dependency before it
    validates a body, so the refusal happens before the request is read — which is
    also why an unauthenticated write cannot be used to probe the validation rules.
    Sweeping the *published* inventory rather than a hand-written list is what makes
    this catch a future route that forgot the dependency.

    The geo reads are in the sweep, and their 401 is the point Phase 7 has to make:
    an anonymous caller is refused before the query is even parsed, so a missing
    cookie short-circuits a malformed radius rather than leaking which queries are
    well formed.
    """
    async with api_harness(tmp_path) as api:
        protected = [(method, path) for method, path in operations(
            api.app, under=API_V2_PREFIX) if (method, path) not in PUBLIC_OPERATIONS]

        assert len(protected) == 51
        for method, template in protected:
            response = await api.client.request(method, concrete(template))

            assert response.status_code == 401, (method, template)
            assert response.json()["error"] == "not_authenticated", template
            assert "set-cookie" not in response.headers, template

        assert api.users.users == {}
        assert api.sessions.sessions == {}
        assert api.profiles.profiles == {}
        assert api.searches.searches == {}
        assert api.companies.companies == {}
        assert api.discoveries.records == {}


@pytest.mark.asyncio
async def test_no_response_schema_reachable_from_v2_declares_a_credential_field(
        tmp_path):
    """Asserted on the contract, not on one response body that happened to be empty.

    The generated TypeScript client is built from this document, so a credential
    field here would become a typed field the frontend could store, log or render.
    The walk follows `$ref` into the nested models — `SignedInResponse` holds
    `AccountResponse`, which is where a `password_hash` would appear if the response
    models were ever built from the domain object instead of from the API schema.

    The second half is the anti-vacuity check: the register and login *bodies* must
    still declare `password`, which proves the walker finds field names at all.
    """
    async with api_harness(tmp_path) as api:
        responses = schema_property_names(api.app, under=API_V2_PREFIX)
        bodies = schema_property_names(api.app, under=API_V2_PREFIX,
                                       section="requestBody")

        assert len(responses) == len(V2_OPERATIONS)
        for operation, fields in responses.items():
            offending = [field for field in fields
                         if any(word in field.lower()
                                for word in FORBIDDEN_IN_RESPONSES)]
            assert offending == [], f"{operation} exposes {offending}"

        assert "password" in bodies["POST /api/v2/auth/register"]
        assert "password" in bodies["POST /api/v2/auth/login"]
        assert "user_id" not in bodies["PUT /api/v2/me/profile"]
        assert "user_id" not in bodies["POST /api/v2/me/search-profiles"]


@pytest.mark.asyncio
async def test_v1_still_answers_exactly_as_it_did_beside_the_v2_router(tmp_path):
    """No session, no CSRF header, no cross-site check, and V1's own 422 body.

    V1 has no accounts, so every one of its routes has to keep working for a caller
    with no cookie — which is why `current_session` and `reject_cross_site_writes`
    are dependencies of the V2 router and not middleware. Middleware is the obvious
    way to write a CSRF check and it would have broken all 29 of these.

    The 422 is the sharpest of the four: V2 redacts the rejected value, V1 keeps
    FastAPI's `input` key. `install_v2_error_handlers` registers one handler for the
    whole application, so it has to delegate for a V1 path, and this is the assertion
    that it does.
    """
    async with api_harness(tmp_path) as api:
        v1 = [(method, path) for method, path in operations(api.app, under="/api/")
              if not path.startswith(API_V2_PREFIX)]

        assert len(v1) == V1_OPERATIONS
        assert len({path for _, path in v1}) == V1_PATHS

        assert (await api.client.get("/api/overview")).status_code == 200

        write = await api.client.post("/api/jobs/1/status",
                                      json={"status": "approved"})
        assert write.status_code == 409, "a V1 write must not need an X-CSRF-Token"
        assert write.json() == {"detail": {"error": "invalid_transition",
                                          "current": None, "target": "approved"}}
        cross_site = await api.client.post("/api/jobs/1/status",
                                           json={"status": "approved"},
                                           headers={"Sec-Fetch-Site": "cross-site"})
        assert cross_site.status_code == 409, "the V1 surface is not CSRF-checked"

        invalid = await api.client.post("/api/jobs/1/status", json={})
        assert invalid.status_code == 422
        assert invalid.json() == {"detail": [{"type": "missing",
                                             "loc": ["body", "status"],
                                             "msg": "Field required",
                                             "input": {}}]}





