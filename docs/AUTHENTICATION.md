# V2 Authentication

Email and password accounts, server-side sessions in PostgreSQL, and a
double-submit CSRF check, introduced in Phase 4.

This document is the reference for the decisions the code cites by section name:
what a session is, why it is a row rather than a signed token, what the two cookies
carry, how an unsafe request is protected, and what is deliberately not disclosed to
a caller who cannot prove they own an account.

V1 is untouched. Its 29 `/api/**` operations still answer with no session cookie and
no CSRF header, because both checks are FastAPI dependencies of the V2 router
(`backend/app/api/router.py`) rather than middleware. Middleware is the obvious way
to write a CSRF check and it would have broken every V1 route at once.

## Layout

```
backend/app/core/passwords.py      Argon2id hashing, the length band
backend/app/core/tokens.py         minting and SHA-256 digesting of tokens
backend/app/core/settings.py       AuthSettings (cookie + lockout policy)
backend/app/domain/user.py         User, UserSession, UserStatus
backend/app/services/authentication.py   register, log_in, authenticate, log_out
backend/app/services/onboarding.py       profile, saved searches, completion
backend/app/api/dependencies.py    current_session, CSRF, cross-site refusal
backend/app/api/cookies.py         the two Set-Cookie headers and their clearing
backend/app/api/errors.py          status codes, error codes, the redacted 422
backend/app/api/routes/auth.py     register, login, session, logout
backend/app/api/routes/me.py       the candidate profile and the saved searches
backend/app/api/routes/onboarding.py     state and completion
backend/migrations/versions/rev_0003_phase_4_identity_and_saved_searches.py
```

The dependency direction is the one Phase 1 established: the domain imports nothing
from infrastructure, the services depend on repository `Protocol`s rather than on a
session, and the API layer is the only place that knows about HTTP.

## Why server-side

A session is a row in `user_sessions`, and the cookie holds a random token that
identifies it. It is not a JWT, and there is no signing key anywhere in
`AuthSettings`.

The reason is revocation. A self-contained signed token is valid until it expires,
because nothing is consulted when it is presented; logging out, disabling an account
or reacting to a stolen laptop then means maintaining a deny-list — which is the
server-side session table again, with an extra format in front of it. A row that can
be `UPDATE`d is the simpler thing that does the job.

The cost is one indexed lookup per authenticated request. That is a primary-key-class
query on `uq_user_sessions_token_digest`, and it buys immediate revocation, an
absolute lifetime, and a session table an operator can read.

## Sessions

`log_in` and `register` both mint two tokens with `secrets.token_urlsafe(32)` — one
session token, one CSRF token — and store **only their SHA-256 digests**, as 64
lower-case hex characters. The raw values exist in exactly two places in their life:
the response that sets the cookies, and the request the browser sends back.

SHA-256 rather than Argon2, deliberately: a password is a low-entropy secret a human
chose and must be slow to verify, while a 32-byte token is 256 bits of uniform
randomness with no dictionary to guess from. A slow KDF on the session lookup would
cost ~100 ms on *every* authenticated request and defeat nothing.

Hashing them at all is about the dump, the backup and the log line: a table of raw
session tokens is a set of working credentials, and a table of digests is not.

Four properties hold for every session:

- **The lifetime is absolute.** `expires_at = issued_at + session_hours` (168 hours
  by default) and nothing moves it. `last_seen_at` is bookkeeping, refreshed at most
  once every five minutes, and refreshing it cannot extend the session — which is
  what makes a stolen cookie finite and what makes the touch safe on a `GET`.
- **Expiry is enforced by the server.** A cookie's `Max-Age` is advice to a browser;
  an attacker holding a copied token ignores it. `UserSession.is_usable` is what
  refuses the request, and an expired row is left in place — sweeping it is a
  background job's business, not a request's.
- **Revocation is immediate.** `POST /auth/logout` sets `revoked_at` and only then
  clears the cookies. Clearing a cookie asks a browser to forget a credential that
  would otherwise keep working for anybody who copied it, so the order matters.
  Revocation is scoped by user id as well as session id, so no id can revoke
  somebody else's session.
- **Disabling an account ends its sessions without touching them.** `authenticate`
  re-reads the account on every request and refuses anything that is not `ACTIVE`,
  so one `UPDATE users SET status = 'DISABLED'` is enough.

Logging in somewhere else does not revoke anything: a phone and a laptop hold two
sessions, and that is the intended behaviour. `log_out_everywhere` exists in the
service, with no route in Phase 4 — it is what a password change will have to call.

### The two cookies

| | session cookie | CSRF cookie |
|---|---|---|
| default name | `__Host-jobsearch_session` | `__Host-jobsearch_csrf` |
| `HttpOnly` | yes | **no**, by design |
| `Secure` | `AuthSettings.cookie_secure` | same |
| `SameSite` | `lax` (configurable, `strict` allowed) | same |
| `Path` | `/` | `/` |
| `Domain` | absent | absent |
| `Max-Age` | seconds left in the session | same |

The CSRF cookie is readable by JavaScript because the frontend has to echo it in a
header. It is a *different* random value from the session token — never the session
token, and the database refuses a session whose two digests are equal
(`ck_user_sessions_digests_are_independent`).

`__Host-` is not decoration. A browser only honours the prefix when the cookie is
`Secure`, has `Path=/` and carries **no** `Domain` attribute, and the effect is that
no sibling subdomain can overwrite it. `Path=/` and the absent `Domain` are therefore
fixed in `backend/app/api/cookies.py` rather than configurable: making them settings
would only create combinations a browser rejects. The prefix is dropped when
`cookie_secure` is off, because a non-`Secure` `__Host-` cookie is discarded.

### `Secure` is a deployment setting, never an inference

`AuthSettings.cookie_secure` defaults to `True` and is **never** derived from
`request.url.scheme`.

Behind a TLS-terminating proxy the browser speaks HTTPS to the edge and the edge
speaks HTTP to the application. A flag inferred from the request scheme would then be
dropped precisely where it is needed, and the downgrade would be silent. If trusted
proxy headers are supported later they may inform URL generation; cookie security
stays an explicit deployment decision.

There is exactly one supported way to run without it, and it is named so it can be
grepped: `AuthSettings.for_local_http()`, which also drops the `__Host-` prefix. It
exists because a cookie jar that has accepted a `Secure` cookie will not send it back
over `http://` — a developer running the production policy on `localhost` gets a
login that appears to succeed and then does not.

## CSRF

Two independent controls, and neither is trusted alone.

**A double submit compared against server-side state.** Every unsafe method — that
is, anything outside `GET`, `HEAD` and `OPTIONS` — must carry `X-CSRF-Token`, and the
value is compared with `secrets.compare_digest` against the digest stored *on the
session row*. A plain double submit compares the header against the cookie, which
anything able to write the cookie satisfies: a compromised sibling subdomain can set
a cookie on the parent domain even though it cannot read the session's server-side
state. Comparing against the row is what makes writing the cookie worthless. The
refusal is `403 csrf_failed`, and it changes nothing — in particular it does not
revoke the session, which would be a denial of service any third-party page could
trigger.

**`Sec-Fetch-Site: cross-site` is refused at router level.** `login` and `register`
have no session yet and therefore no CSRF token to check, so the header is what
closes them against login CSRF — an attacker's page signing a visitor into an account
the attacker controls. The check is a dependency of the whole V2 router, so it runs
before a body is read.

A **missing** `Sec-Fetch-Site` header passes. `curl`, the test suite and every
server-to-server client send no such header, and rejecting them would be rejecting
the API's own callers; the double submit is what protects an authenticated write.
"Reject unless same-site" is the tempting change to make here, which is why there is
a test that pins the current behaviour.

### What `SameSite=Lax` actually contributes

`Lax` withholds the cookie on cross-site *subresource* requests and on cross-site
requests made with an unsafe method, including a top-level form POST from another
origin. What it permits is a cross-site top-level **navigation using a safe method** —
following a link. So the classic cross-site form POST is already stopped by the
attribute, and `Strict` is not needed for that; `Strict` is about withholding the
cookie even on an inbound link, which would land a user on the site logged out.

That makes `SameSite` defence in depth rather than the CSRF control:

- it is a browser behaviour, so it does nothing for a non-browser client, an old
  browser, or a browser bug;
- it says nothing about a *same-site* attacker, which is exactly the case the
  server-side digest comparison covers.

Both are kept. Neither is described as sufficient.

### No `GET` mutates state

The other half of a CSRF story is that a safe method must not change anything, since
no CSRF token is required of one and `Lax` still sends the cookie on a top-level
navigation. The four V2 `GET`s are reports; `POST /onboarding/complete` exists so that
the screen displaying onboarding progress is not the thing that records it.

The single exception is `last_seen_at`, and it is bounded on purpose: at most one
write every five minutes, on the caller's own session row, idempotent, and — because
`expires_at` is absolute — incapable of extending anything. `tests/test_v2_api_surface.py`
asserts both halves: that only the read routes answer a safe method, and that calling
every one of them twice leaves all four stores identical.

## Passwords

Argon2id, with argon2-cffi's own defaults: `m=64 MiB, t=3, p=4`, the second parameter
set RFC 9106 recommends. The numbers are deliberately not hard-coded — a dependency
bump then raises the cost, and `verify_password` returns `needs_rehash` alongside
`matched`, so the next successful login re-hashes a password stored under older
parameters. That is the one moment rehashing is free: the plaintext is in hand.

bcrypt was not chosen because it truncates at 72 bytes, and PBKDF2 because it is
cheap on a GPU. Neither is worth choosing for a new table.

Length is the only rule: at least 12 characters, at most 1024. There are no
composition requirements — NIST SP 800-63B advises against them, because they push
users towards `Password1!` and rule out long passphrases for no measured gain. The
maximum is a denial-of-service bound: Argon2 will happily spend a second hashing a
one-megabyte string that anybody can post.

The band is enforced twice: on `RegisterRequest`, so the form gets a field-level 422,
and inside `hash_password`, so no service or future CLI can store a one-character
password by going round the HTTP layer. `LoginRequest` deliberately carries **no**
band — a login form that rejected a password for being too short would be publishing
the rule and refusing a password that may predate it, and the answer to a wrong
password is the same either way.

## Lockout

Ten consecutive failures lock an account for fifteen minutes
(`AuthSettings.max_failed_logins`, `lockout_minutes`). Three properties matter more
than the numbers:

- **It is temporary.** A permanent lock on failed logins is a denial of service
  anybody can trigger against a known address.
- **It does not compound.** Attempts made *during* a lock are refused without being
  counted, so an attacker cannot extend somebody else's lock indefinitely.
- **A lapsed lock resets the counter.** Otherwise the first typo after a lockout
  would re-lock immediately, and an account that once hit the limit would have an
  allowance of one for ever.

A wrong password is answered `401 invalid_credentials` throughout — including the
attempt that trips the lock. `423 account_locked`, with `locked_until`, is only ever
returned to a caller who supplied the **correct** password: at that point they have
demonstrated ownership, and telling them how long to wait is help rather than a leak.

## Enumeration

An unknown address and a wrong password produce byte-identical answers: `401` with
`{"error": "invalid_credentials", "detail": "that email address and password do not
match an account"}`, and no `Set-Cookie`. So do a locked account and a disabled one,
*unless the password was correct*.

`log_in` verifies the password **before** consulting the account's state, which is
what makes that hold. The timing is equalized too: on the "no such account" path
`spend_verification_time` runs a real Argon2 verification against a throwaway digest,
because a login for an unregistered address that returns in a fraction of the time is
a working oracle — and the identifier here is an email address.

Registration is the one place existence is necessarily disclosed. Refusing a duplicate
address requires saying so (`409 email_already_registered`), and the alternative — 
answering 201 and sending mail — needs email delivery, which Phase 4 does not include.

## Errors

Every `/api/v2` refusal has the same envelope: an `error` code a client can branch on
and a `detail` sentence for a human. Codes, not sentences, are the contract.

| Status | `error` | When |
|---|---|---|
| 401 | `not_authenticated` | no session cookie, or one that is unknown, expired, revoked, or belongs to a non-`ACTIVE` account |
| 401 | `invalid_credentials` | unknown address, or wrong password |
| 403 | `csrf_failed` | unsafe method without a matching `X-CSRF-Token`, or `Sec-Fetch-Site: cross-site` |
| 403 | `account_disabled` | correct password, account not `ACTIVE` |
| 404 | `candidate_profile_not_found` | no profile saved yet — the frontend sends the user to onboarding |
| 404 | `search_profile_not_found` | no such saved search, **or** somebody else's |
| 409 | `email_already_registered` | registration with a taken address |
| 409 | `onboarding_incomplete` | completion attempted without a profile and an active search; carries `has_profile` and `active_search_profiles` |
| 409 | `conflict` | a database uniqueness race |
| 422 | `validation_failed` | a rejected body, with the values removed (below) |
| 423 | `account_locked` | correct password during a lockout; carries `locked_until` |
| 503 | `database_unavailable` | PostgreSQL unreachable |

`404` rather than `403` for another account's saved search is a deliberate choice: a
`403` would confirm that the id exists.

## What is never written down

Not in a response body, not in a log line, not in a traceback:

- raw session tokens and raw CSRF tokens;
- passwords;
- password hashes and token digests.

Three mechanisms enforce it rather than one convention. Every secret is a
`SecretStr`, so it cannot reach a log or a `model_dump_json()` without an explicit
`get_secret_value()`. The API response models are hand-written schemas rather than
domain objects, so there is no field for a credential to travel in — asserted against
the OpenAPI document, not against one sampled response. And the `/api/v2` validation
handler rebuilds FastAPI's 422 body as `{type, loc, msg}` per error, dropping `input`
and `ctx`, because FastAPI's default `input` key carries the value it rejected — which
on a registration request is a password.

V1's 422 is untouched, `input` included: it is part of V1's published behaviour, and
the handler delegates for any path outside `/api/v2`.

## Authorization

There is no `/api/v2/users/{id}/…` route, and that is the authorization model rather
than a naming style: **the owner is never in the path.** It comes from the session, so
a request cannot ask about somebody else's data even incorrectly.

The `{search_profile_id}` routes do not check ownership themselves either. They hand
the id and the session's user to the service, whose repository scopes the query, and a
search belonging to another account comes back absent and turns into the same 404 as
one that never existed. A check in the handler would be a second place for the rule to
live, and the two would eventually disagree.

The request models make the same rule structural: `CandidateProfileDraft` and
`SearchProfileDraft` have no `user_id` field, and `extra="forbid"` means a body that
invents one is rejected with a 422 rather than having the key silently dropped.

## Onboarding

An account is usable once it has a candidate profile and at least one **active** saved
search. `GET /api/v2/onboarding` reports `has_profile`, `search_profiles`,
`active_search_profiles`, `completed_at`, `is_complete` and `may_complete`; the counts
are numbers rather than booleans because "one search, paused" is a different screen
from "no searches".

`POST /api/v2/onboarding/complete` takes no body at all. Both preconditions are
re-read from the database, so there is nothing a client could send that would make it
succeed, and a request that skipped a step gets `409 onboarding_incomplete` carrying
the same two numbers the `GET` reports — the button and the refusal cannot disagree.
It is idempotent and keeps the **first** timestamp: when onboarding finished is a
historical fact, and a double-submitted button must not rewrite it.

Registration signs the browser in on purpose. The next screen is onboarding, so the
201 has to be a usable session rather than a confirmation.

## Schema

Revision `0003` completes `users` and `candidate_profiles`, and creates
`user_sessions`, `candidate_languages`, `candidate_work_authorizations`,
`candidate_availability_slots`, `search_profiles` and `search_areas`. The conventions
— TIMESTAMPTZ in UTC, UUID primary keys, explicit ownership, forward-only revisions —
are in docs/PERSISTENCE.md; what is specific to authentication is:

- `uq_users_email` and `ck_users_email_normalized`, so an address is unique and stored
  in the normalized form the login lookup uses;
- `uq_user_sessions_token_digest`, the index the per-request lookup rides on;
- `ck_user_sessions_token_digest_format` and its CSRF twin, `^[0-9a-f]{64}$`, so a raw
  43-character token cannot be stored where a digest belongs;
- `ck_user_sessions_digests_are_independent`, so the CSRF token can never be the
  session token;
- `ck_user_sessions_window_is_forward` and `ck_user_sessions_last_seen_after_issued`.

## Tests

Two layers, and they assert different things. The service tests
(`tests/test_v2_authentication.py`, `tests/test_v2_onboarding.py`) cover the
decisions. The request-flow tests drive the real application over HTTP through
`httpx.ASGITransport` and cover what a browser meets — cookie attributes, status
codes, error codes — with `AuthenticationService` and `OnboardingService` overridden
onto in-memory fakes, so no PostgreSQL is involved and `Clock` makes expiry and
lockout instant.

The nine properties Phase 4 had to demonstrate, and where each one lives:

| Property | Module |
|---|---|
| cookie attributes | `tests/test_v2_api_auth.py` |
| CSRF rejection | `tests/test_v2_api_auth.py` |
| logout / revocation | `tests/test_v2_api_auth.py` |
| expired session rejection | `tests/test_v2_api_auth.py` |
| locked account behaviour | `tests/test_v2_api_auth.py` |
| no auth or session secret in a response | `tests/test_v2_api_auth.py`, `tests/test_v2_api_surface.py` |
| cross-user isolation | `tests/test_v2_api_me.py` |
| unauthenticated `/api/v2/**` | `tests/test_v2_api_surface.py` |
| V1 behaviourally unchanged | `tests/test_v2_api_surface.py` |

`tests/v2_api.py` is the harness. Two details in it are worth knowing before writing a
new test: the base URL has a dotted host (`jobsearch.test`) because `http.cookiejar`
files a `Domain`-less cookie under `testserver.local` for a dotless host, which makes
replacing a cookie by hand impossible; and cookie attributes are parsed from the raw
`Set-Cookie` header, because the jar keeps the value and models neither `HttpOnly` nor
`SameSite`.

## Frontend contract

The Nuxt application in `frontend/` is the only browser client, and its half of the
scheme lives in four files: `app/utils/api-client.ts`, `app/stores/session.ts`,
`app/middleware/auth.ts` and `app/utils/v2-errors.ts`. What follows is the part of
each decision the backend cannot enforce for it.

**One cookie is readable, the other must not be.** `csrfToken()` reads
`document.cookie` and tries `__Host-jobsearch_csrf` before `jobsearch_csrf`, in that
order, so a bare cookie planted by a sibling subdomain cannot displace the real token.
The session cookie appears nowhere in the frontend: it is `HttpOnly`, the browser
attaches it, and `credentials: 'same-origin'` is written out on every call because the
app is served by the FastAPI process it calls. The CSRF value is re-read per request
rather than held in memory — a logout in another tab clears the jar, and a remembered
token would then be sent forever against a session that no longer exists.

**`X-CSRF-Token` on every unsafe method.** The client's `SAFE_METHODS` is the same set
as the dependency's, and the header is added for everything outside it, including the
multipart `POST` to `/api/transcribe` (where the browser, not the client, sets
`Content-Type`). It is sent unconditionally rather than per-surface: V1's routes ignore
an unknown header, and a client that decided per URL would be a second copy of the
routing table.

**401 is an answer, not a failure.** `load()` treats it as `anonymous`; anything else —
a 503 from an unreachable database, a dropped connection — leaves `status` at `unknown`
and records the cause, so the next navigation asks again instead of telling a signed-in
user they are signed out on the strength of a network blip. That is why `status` has
three values: "not asked yet" is not "nobody", and collapsing them is what flashes a
login form on every page load. There is deliberately **no** interceptor that redirects
on a 401 from a write. A refusal surfaces as `ApiError` and `v2-errors.ts` turns its
`error` slug into one sentence beside the form; a redirect from inside a mutation would
throw away what the user had typed.

**A named guard, not a global one.** `definePageMeta({ middleware: 'auth' })` appears on
`/profile` and `/onboarding` only. The V1-ported screens — overview, jobs, analytics,
settings — stay unguarded because the 29 V1 operations behind them have no accounts, so
a guard there would demand a login the data cannot use. A global middleware with an
exception list would invert the default and make the list the thing to remember. The
guard forwards `to.fullPath` as `?redirect=`, and the login page honours it only when it
starts with a single `/` — an absolute URL there would be an open redirect out of a
mailed link. An account with `onboarding_completed_at === null` goes to `/onboarding`
whatever it asked for. None of this is the protection: the API refuses an
unauthenticated request itself, and the guard only chooses which screen to render.

**The client cache is part of cross-user isolation.** `adopt()` and `forget()` both call
`clearCached('me', 'onboarding')`, so the previous account's profile and saved searches
cannot outlive its session — signing in must not inherit them either.
`invalidate()` would be wrong here: it refetches, which after a logout is a burst of
401s, and it only reaches keys still mounted, while a logout has already navigated away.

**What the browser suite proves.** `tests/nuxt/` covers the decisions with a stubbed
`fetch` — `middleware/auth.spec.ts`, `stores/session.spec.ts`, the four
`pages/{login,register,onboarding,profile}.spec.ts`, the two form components, and
`composables/useApiQuery.spec.ts` for the cache rules above. `tests/e2e/auth.spec.ts`
covers the two things only a real browser shows: that `X-CSRF-Token` actually leaves it,
and that the router obeys the guard's `navigateTo`. Neither layer runs a backend; the
e2e cookie carries the bare `jobsearch_csrf` name because the dev server is HTTP, which
is `AuthSettings.for_local_http()`'s case.

## Not in Phase 4

Deliberately absent, and none of it is a gap to be filled opportunistically:

- email verification and password reset — both need mail delivery;
- password change and `log_out_everywhere` as routes (the service method exists);
- OAuth, SSO, MFA and passkeys;
- rate limiting per IP (the lockout is per account);
- roles or permissions beyond ownership: every account owns its own data and nothing
  else;
- refresh-token rotation, which a server-side session does not need.
