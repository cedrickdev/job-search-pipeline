# Credential security

How the V2 platform stores the secrets it must keep, and — the part that makes "keep"
safe — how it rotates the key that protects them. The governing rule is that a
deployment secret lives in the environment (a secret manager in production), and the
database holds only what is unavoidably per-user: today that is exactly one thing, a
remote LLM connection's API key, and it is stored encrypted with a key the database
never sees.

This document is the operator's reference for Phase 16 §19–22: the credential
inventory, the versioned secret vault the encryption reuses, and the rotation runbook.

## Credential inventory

Every credential surface in the system, and where it is allowed to live. The
distinction is deliberate: a deployment secret in the database is a secret one `pg_dump`
away from a leak, so only a credential that is inherently per-user and must survive a
restart is persisted — and then only encrypted.

| Surface | Where it lives | Notes |
| --- | --- | --- |
| LLM connection API key | **Encrypted in PostgreSQL** (`llm_connections.encrypted_api_key` + `secret_version`) | The only reversible per-user credential V2 persists. Fernet ciphertext; the key is env-only (below). Surfaced to a client only as `has_api_key`. |
| Master encryption key | Env / secret manager (`JOBSEARCH_LLM_SECRET_KEY`, `…_VERSION`, `…_V<N>`) | Read once at startup; never a column, never logged, never in an API response. Protects the row above. |
| Stripe secret + webhook secret | Env only (`JOBSEARCH_STRIPE_SECRET_KEY`, `JOBSEARCH_STRIPE_WEBHOOK_SECRET`) | Billing adapter credentials. Deployment secrets, not per-user. |
| Database credentials | Env only (`JOBSEARCH_DATABASE_URL` / `POSTGRES_*`) | A process reaches the database with these; the database does not store them. |
| Geocoder API key | Env only (indirect: `GEOCODER_API_KEY` names the var that holds it) | The public Nominatim server needs none. |
| Discovery source keys (Jooble) | Env only (`JOOBLE_API_KEY`) | A discovery adapter's key. |
| Password | **Hashed** in PostgreSQL (Argon2id, one-way) | Not a reversible secret — verified, never decrypted. No vault applies. |
| Session / CSRF tokens | **Digested** in PostgreSQL (SHA-256, one-way) | Bearer tokens stored as digests, not recoverable. No vault applies. |
| Site/ATS account password | **V1 only** (`SITE_ACCOUNT_PASSWORD` → `data/site_credentials.json`, plaintext) | No V2 workflow reads this file. See "No plaintext site-credential dependency" below. |

Two things follow. First, the only ciphertext a rotation touches is the LLM connection
key — everything else is either env-only or a one-way hash. Second, nothing in the
"env only" rows belongs in the database "for convenience"; putting a deployment secret
there would trade a secret-manager boundary for a `pg_dump`.

## The versioned secret vault

Encryption is not reinvented for Phase 16 — it reuses the Phase 11 vault verbatim
(`backend/app/llm/secrets.py`, docs/LLM_CONNECTIONS.md). The relevant facts:

- **Fernet**, an established AEAD (AES-128-CBC + HMAC-SHA256). A tampered ciphertext
  fails to decrypt rather than yielding garbage a caller might send to a provider.
- **The master key is env-sourced**, read once into a `FernetSecretCipher`. It is never
  a column, never returned, never logged. A database dump holds ciphertext and a version
  tag and nothing that decrypts them.
- **Every ciphertext carries a `secret_version`** naming the key that wrote it. That tag
  is what lets a rotation re-encrypt old rows without guessing which key each used: a
  value at version *N* is decryptable by the key registered under *N*.

`FernetSecretCipher` already holds an active key plus any number of older keys keyed by
version, so a value written under an earlier version still decrypts while a rotation
brings it forward. `encrypt` always writes at the active version. This is the single
mechanism; where a future production credential must be persisted, it is encrypted
through this vault rather than a second scheme.

## Rotating the master key

Rotation retires an old master key by re-encrypting every stored credential under a new
one. It is driven by `python -m backend.app.cli.rotate_credentials`
(`backend/app/cli/rotate_credentials.py`), which walks every credential across every
account — the one operator-scoped credential path in the system — and, for each row a
previous key wrote, decrypts under that key, re-encrypts under the active key, and
verifies the new ciphertext reads back to the same plaintext *before* it writes.

Three invariants make it safe to run and safe to re-run:

- **Idempotent.** A row already at the active version is left untouched, so a second run
  (or a run after a crash) does nothing and reports the same state.
- **Fail-closed, never destructive.** A row whose previous key is not configured cannot
  be decrypted; it becomes a typed failure naming the connection and version — never the
  value — and the row is left exactly as it was. Supply the key and re-run; no credential
  is lost.
- **No silent downgrade.** A row stored under a version newer than the active key is a
  failure, not a write: rotating it down would weaken it. Raise the active version first.

The plaintext exists only for the instant between decrypt and re-encrypt, as a
`SecretStr` that does not render in a traceback. It never enters the report, the store,
or a log — rotation never reads a secret into output.

### Runbook

1. **Generate the new key** with the library that consumes it:
   ```
   python -c "from backend.app.llm.secrets import generate_master_key; print(generate_master_key())"
   ```
   Store it in your secret manager.
2. **Stage the rotation window** in the environment. If the current active version is 1:
   ```
   JOBSEARCH_LLM_SECRET_KEY=<the new key>
   JOBSEARCH_LLM_SECRET_KEY_VERSION=2
   JOBSEARCH_LLM_SECRET_KEY_V1=<the old key>
   ```
   The new key is active at version 2; the old key stays available under version 1 so
   its rows still decrypt during the rotation.
3. **Dry-run first:**
   ```
   python -m backend.app.cli.rotate_credentials --dry-run
   ```
   A dry run performs the full decrypt / re-encrypt / verify for every stale row but
   writes nothing. A clean dry run (zero failures) proves the configured keys can
   actually rotate the table — so the applied run will not leave a credential behind.
4. **Apply:**
   ```
   python -m backend.app.cli.rotate_credentials
   ```
   The report gives counts and, for any failure, the connection id, its stored version,
   and a fixed reason — never a key or a credential.
5. **Verify** the report shows `failed 0`. If not, the reasons name which previous keys
   are missing; supply them (as `…_V<N>`) and re-run — the already-rotated rows are
   idempotently skipped.
6. **Retire the old key.** Once an applied run reports zero failures, remove
   `JOBSEARCH_LLM_SECRET_KEY_V1` from the environment and delete the old key from the
   secret manager. The old master key is now retired.

### Exit codes

Mirroring the other CLIs: `0` clean, `1` misconfigured (no active key is set — nothing
to rotate *to*, so it refuses rather than run a no-op that looks like success), `3`
finished with failures (some credential could not be rotated; supply its previous key
and re-run).

## No plaintext site-credential dependency in V2

Phase 16 §21 requires that production V2 not depend on plaintext credential files. It
does not. The V1 applier persisted employer-portal account passwords to
`data/site_credentials.json` in plaintext (`SITE_ACCOUNT_PASSWORD`,
`pipeline/site_credentials.py`); that path is reachable only from V1 code. The V2
application engine does not read it — the V2 browser adapter is an isolation placeholder
that returns `REQUIRES_HUMAN` rather than driving a login, and the task dispatcher wires
only paths and a child environment, never that file. So there is no V2 workflow whose
correctness depends on a plaintext site credential. Should V2 ever need to persist a
site credential, it goes through the versioned vault above, exactly as the LLM key does
— not a plaintext file.

## Not covered here

Data-subject lifecycle — export, deletion, retention, and the "GDPR-aware" posture — is
credential-adjacent but distinct, and is documented with those milestones (Phase 16
M5–M7), not here. This document is scoped to credential storage and key rotation.
