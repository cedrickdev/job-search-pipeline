# LLM Connections

Built in Phase 11, alongside the provider-neutral runtime it configures (see [LLM
Provider Architecture](./LLM_PROVIDER_ARCHITECTURE.md)). This document describes the
*settings surface*: how a user stores, edits, enables and probes the connections that
tell the platform **where a prompt could go**. It configures the router's candidates;
it never runs one.

The point of the layer in one sentence:

> A connection is a user-owned, authorization-scoped record of a way to reach an LLM
> provider — a provider type, an endpoint, a model, and (for a hosted gateway) an API
> key held encrypted at rest. The API accepts a credential but never returns it, and
> there is no endpoint that sends a prompt through a connection.

```
settings form ──▶ CreateLLMConnectionRequest ──▶ LLMConnectionService.create
                    (api_key in the body)             │  encrypts the key (SecretCipher)
                                                       ▼
                                              LLMConnection (row)  ── encrypted_api_key + secret_version
                                                       │
LLMConnectionResponse ◀── has_api_key (never the value) ┘

POST /{id}/healthcheck ──▶ LLMConnectionService.healthcheck
                             │ builds the live provider via LLMProviderFactory
                             ▼
                          ProviderHealth (data, not stored)
```

## Where it lives

```
backend/app/llm/connection.py              LLMConnection, LLMProviderType (the value object + shape rules)
backend/app/llm/secrets.py                 SecretCipher, FernetSecretCipher, generate_master_key
backend/app/services/llm_connections.py    LLMConnectionService, its drafts, updates and exceptions
backend/app/api/routes/llm.py              /api/v2/settings/llm — the eight operations
backend/app/api/schemas.py                 Create/Update/Set requests, the Response/List/Health models
backend/app/api/errors.py                  the slug → HTTP status mapping, incl. the LLMError table
backend/app/api/dependencies.py            llm_secret_settings, llm_cipher, llm_connection_service
backend/app/infrastructure/database/
  models.py                                LLMConnectionRow (and ProviderSessionRow, LLMRunRow)
  mappers.py                               connection ↔ row
backend/app/repositories/
  sqlalchemy_repositories.py               LLMConnectionRepository (owner-scoped reads, clear_default)
backend/migrations/versions/rev_0008_phase_11_llm_platform.py
frontend/app/composables/useLlmConnections.ts   the query + the action mutations
frontend/app/components/LlmConnectionForm.vue    the create/edit form
frontend/app/pages/providers.vue                 the /providers screen
```

## The invariants

Four rules, each held by a test rather than by review:

- **The owner comes from the session, never the body.** Every method on
  `LLMConnectionService` takes a `user_id` and reaches the repository through it, so a
  connection is created under, read by, edited by and deleted from only the account
  behind the cookie. A connection id that belongs to another account reads as *absent*:
  the service raises the same `LLMConnectionNotFound` for "no such connection" and "not
  yours", so a caller cannot enumerate another account's connections by id, nor probe
  them by trying to delete one.
- **The credential is write-only, end to end.** `POST` and `PATCH` accept an `api_key`;
  no response model has a field for the value or its ciphertext, only `has_api_key`.
  The plaintext is encrypted before it is stored and never lingers — it exists as a
  `SecretStr` for the moment the service hands it to the cipher, and again only for the
  instant the factory decrypts it to build a provider for a health probe.
- **A key is encrypted at rest with a master key that is never in the database.** The
  key comes from the `JOBSEARCH_LLM_SECRET_KEY` environment variable; a row holds the
  Fernet ciphertext and a `secret_version` and nothing that decrypts them. A deployment
  with no master key can still manage CLI and keyless local connections — the service
  refuses *only* the one write that would need a key it cannot encrypt.
- **There is exactly one default connection per account.** Marking a connection default
  clears the flag on the account's others first, in the same request, and a partial
  unique index (`WHERE is_default`) makes "two defaults" unstorable — so "which
  connection does a task use by default" always has one answer.

## The value object and its shape rules

`LLMConnection` (`backend/app/llm/connection.py`) is the single owner of what a valid
connection looks like — the service builds the model and translates its refusal, it
does not restate the rules. `LLMProviderType` has four members, split by transport:

| `LLMProviderType` | Transport | Carries |
| --- | --- | --- |
| `CLAUDE_CODE` | CLI | nothing but a display name — the CLI self-authenticates |
| `CODEX` | CLI | nothing but a display name |
| `OPENAI_COMPATIBLE` | remote HTTP | `base_url` (https), `model`, an API key, custom headers |
| `LOCAL_OPENAI_COMPATIBLE` | loopback HTTP | `base_url` (loopback), `model`, custom headers; keyless |

Two shape rules are validators on the model, so a bad shape is a 422 before anything is
written, not a broken row:

- **A CLI connection carries no `base_url`, no `encrypted_api_key` and no
  `custom_headers`.** Injecting a key into a self-authenticating CLI would violate the
  §1 security invariant; the model makes it unconstructible.
- **An API connection requires a `base_url`.** The hostname is never assumed, so it is
  required and never defaulted.

A third rule pairs the credential: `encrypted_api_key` and `secret_version` are stored
together or not at all. All three are also **database CHECK constraints**
(`transport_shape`, `secret_pair`) in `rev_0008`, so the V1 importer, a backfill or a
hand-written `UPDATE` cannot slip a malformed row past Python.

## Encryption at rest

`backend/app/llm/secrets.py` is the only module that encrypts and decrypts. The rules
that make "encrypted at rest" mean something:

- **An established AEAD, never home-grown crypto.** Fernet — AES-128-CBC with an
  HMAC-SHA256 authentication tag — from the `cryptography` library. Authenticated, so a
  tampered ciphertext fails to decrypt (raising `SecretDecryptionError`) rather than
  yielding garbage a caller might send to a provider as a real key.
- **The master key comes from the environment and is never persisted or returned.** It
  is read once at startup into a `FernetSecretCipher`; it is not a column, not part of
  any response, not written to a log.
- **Only the ciphertext and a `secret_version` are stored.** The version tags which key
  encrypted a value (`CURRENT_SECRET_VERSION = 1` today), so a rotation can register a
  second cipher under version 2 and re-encrypt version-1 rows without guessing which
  key each used.

`SecretCipher` is a `Protocol`, so a test substitutes a trivial in-memory cipher and
the persistence tests need no key ceremony; production wires `FernetSecretCipher` from
the configured key. `generate_master_key()` produces a fresh url-safe key for an
operator's key ceremony — it is not called at runtime.

`backend/app/api/dependencies.py` builds the cipher lazily and caches it: when
`JOBSEARCH_LLM_SECRET_KEY` is unset it caches a `_NO_CIPHER` sentinel, so the service
is constructed with `cipher=None` and a deployment stays able to manage CLI and keyless
connections while refusing to store a hosted key it could not protect.

## The service

`LLMConnectionService` (`backend/app/services/llm_connections.py`) is the write side.
It holds the owner-scoped repository, the optional `SecretCipher`, and an
`LLMProviderFactory` for the health probe. It takes `now` per write (no clock in the
constructor), so a request's `created_at`/`updated_at` agree.

| Method | Does |
| --- | --- |
| `list_for_user` | this account's connections, in the router's priority-then-id order |
| `get` | one connection, or `LLMConnectionNotFound` (also when not this account's) |
| `create` | encrypt any credential, build the model, clear the old default if this one is default, upsert |
| `update` | a partial edit; three-state credential; **rebuilt through the model**, not `model_copy`, so validators re-run |
| `set_enabled` | toggle on/off without touching the row's stored key |
| `set_default` | clear every other default, then mark this one (in that order, so a crash leaves none rather than two) |
| `delete` | remove it, or `LLMConnectionNotFound` if it was not this account's |
| `healthcheck` | build the live provider and probe it; return `ProviderHealth`, store nothing |

The `update` path's use of the model rather than `model_copy` is deliberate and
tested: a copy does not re-run the validators, so an edit that cleared an API
connection's base URL would slip a broken shape into the store. Rebuilding raises
`LLMConnectionInvalid` (a 422) instead.

### The three-state credential

An edit needs three states for the key, not two:

| The update carries | Effect |
| --- | --- |
| neither `api_key` nor `remove_api_key` | the stored key is **kept** untouched |
| `api_key` set | the key is **rotated** — re-encrypted at the current version |
| `remove_api_key: true` | the key is **cleared** |

`api_key` and `remove_api_key` together is contradictory and refused at the schema (a
422) before the service is called. This is what lets a user edit a gateway's model
without re-typing its key.

### The exceptions and their statuses

| Exception | HTTP | Slug |
| --- | --- | --- |
| `LLMConnectionNotFound` | 404 | `llm_connection_not_found` |
| `LLMConnectionInvalid` | 422 | `llm_connection_invalid` |
| `LLMSecretKeyUnavailable` | 409 | `llm_secret_key_unavailable` |

`LLMConnectionInvalid.messages` are the model validators' own sentences — never the
input that was rejected, because a Pydantic `ValidationError` can quote the offending
value in its representation. A form shows *which rule* failed without echoing a value
back.

## The API

`/api/v2/settings/llm`, eight operations, one authorization model (the owner from the
session). There is no `POST /llm/complete` and no raw-prompt passthrough.

| Method | Path | Does |
| --- | --- | --- |
| `GET` | `/connections` | list this account's connections |
| `POST` | `/connections` | create one (201) — encrypt any key first |
| `GET` | `/connections/{id}` | read one |
| `PATCH` | `/connections/{id}` | partial edit, three-state credential |
| `PUT` | `/connections/{id}/enabled` | toggle on/off |
| `PUT` | `/connections/{id}/default` | make it the account default |
| `DELETE` | `/connections/{id}` | delete it (204; a second `DELETE` is a 404, not idempotent) |
| `POST` | `/connections/{id}/healthcheck` | probe the live provider, return its health |

`LLMConnectionResponse` is the only shape a client sees: the id, the provider type, the
display name, the endpoint, the model, `has_api_key` (a boolean), the headers, the
enabled/default flags, the priority and the timestamps — never a key value. A provider
failure that surfaces through the LLM layer (e.g. a misconfigured local endpoint on a
probe) is a typed, secret-free `LLMError` mapped by `_LLM_STATUS` in `errors.py`:
`503`/`504`/`429` for unavailable/timeout/rate-limited, `409` for
auth/misconfigured/capability, and `502` for anything unmapped.

## Health is a probe, not a stored fact

`POST /{id}/healthcheck` builds the connection's provider through the same factory the
router uses — decrypting the credential for that instant — and asks it. The result is
**data, not an exception**: a provider that is down answers with an `UNAVAILABLE`
status the settings page renders, and its `detail` is a secret-free sentence the
platform composed, never a raw provider message. Health is deliberately **not
persisted**: a stale "healthy" row would mislead more than an unknown would, so the
registry keeps the last health in-process and a settings page reads the current state.

`ProviderHealthStatus` has six members — `UNKNOWN`, `HEALTHY`, `DEGRADED`,
`UNAVAILABLE`, `AUTH_REQUIRED`, `MISCONFIGURED` — and `UNKNOWN` is *usable*: a provider
nobody has probed is given the benefit of the doubt, because refusing to try is how a
healthy-but-unprobed provider becomes invisible.

## The frontend

The `/providers` screen (`frontend/app/pages/providers.vue`) is a **new top-level
route**, not nested under the V1 `/settings` page, matching the other guarded V2
surfaces (companies, map, documents, evidence). It is session-gated with
`middleware: 'auth'`, and the `Providers` nav link appears only for a signed-in
visitor. `frontend/app/composables/useLlmConnections.ts` holds the query
(keyed `llm:connections`) and the create/update/remove/enable/default mutations, each
invalidating that key; the health probe is a mutation that invalidates nothing, because
health is not stored.

`LlmConnectionForm.vue` keeps two rules the backend also keeps, so a bad request rarely
leaves the browser:

- **A CLI type omits the endpoint, key and header fields entirely** — not merely
  disables them — because a CLI carries none of them (§1). An API type reveals all
  three and will not submit without a base URL.
- **The credential is write-only, with three states on an edit.** A connection that
  stores a key shows keep/replace/remove radios — never the value; a create or a
  keyless connection shows a plain optional add-key box, and an empty box is "no
  credential", not an empty string. Provider type is fixed as a label on an edit,
  because it is identity.

`X-CSRF-Token` rides on every write, read fresh from the cookie per request, and no
credential is ever stored client-side. See [Frontend Architecture](./FRONTEND_ARCHITECTURE.md)
§Providers.

## Persistence

`rev_0008` adds `llm_connections`, `provider_sessions` and `llm_runs`, all
`user_id`-scoped and `ON DELETE CASCADE` (a run sets `connection_id` NULL on delete,
because a run is provenance that outlives the connection it used). The connection
CHECKs — `secret_pair` and `transport_shape` — are the data-layer half of the security
model, restating the value object's validators so the invariants hold against every
writer, not only the Python path. See the migration docstring and [V2
Persistence](./PERSISTENCE.md) for the schema conventions the revision follows.

## Tests

No test touches a live LLM, a socket or a real key ceremony (CLAUDE.md §Testing). The
API surface is `tests/test_v2_api_llm.py` (lifecycle; key never returned and stored
encrypted; rotate/clear/keep and the conflict; a CLI needing no key and an API needing
a URL; the single default; enable/disable; cross-user 404; a healthy probe; a
misconfigured local probe; the no-master-key 409; the anonymous 401; the transport
guard), wired through a real `FernetSecretCipher`, an `httpx.MockTransport`
healthcheck, and a fake `LLMConnectionRepository`. The frontend adds
`useLlmConnections.spec.ts`, `providers.spec.ts`, `LlmConnectionForm.spec.ts` and a
three-flow `providers` e2e that proves the screen is reachable, the CSRF header leaves
the browser on a CLI create, and the guard redirects an anonymous visitor.
