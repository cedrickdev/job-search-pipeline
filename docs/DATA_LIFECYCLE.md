# Data lifecycle

What the V2 platform lets an account do with the data it owns: take a copy of it out,
and erase it for good. These are the two operator- and user-facing halves of the
same question — *whose data is this, and can its owner get it back or get rid of it?*
— and Phase 16 answers both under one authorization rule.

This document is the reference for Phase 16 §23–29 and §68–69: the account **export**
(§23–25, §68) and the account **deletion** (§26–29, §69). It states, for each, what
moves or is removed, what is deliberately kept, and why the boundary sits where it does.

> **GDPR-aware, not certified.** These features are built to make the data-subject
> rights the GDPR describes — access (a portable copy) and erasure (deletion) —
> mechanically possible for an account holder. This is an engineering description of
> that mechanism, not a legal compliance claim: the platform does not assert
> certification against any regulation, and an operator's own obligations (a data
> processing agreement, a retention schedule, a lawful basis) are theirs to meet.

## One authorization rule for both

The owner of an export or a deletion is **the session's account, never the request
body or the path** (docs/ENGINEERING_STANDARDS.md §Security, §63). Neither route takes
an account id. A request can therefore only ever act on the account it is signed in as:
you cannot export or delete someone else. This is not a check that could be forgotten
per-route — it falls out of the fact that the owner is read from the authenticated
session, and there is no other id to read.

Both routes are unsafe writes behind the router-level CSRF guard, so a cross-site
request cannot trigger either. Deletion adds a second gate on top of that (below).

## Export — a portable copy (§23–25, §68)

`POST /me/exports` produces a point-in-time archive of the account's own data and
records it as an `AccountExport` moving `PENDING → READY`. `GET /me/exports` lists the
account's exports; the download streams the archive bytes. The archive is written to an
object store keyed by `(user_id, export_id)`, rooted outside the database, so a download
reads real bytes rather than a row.

### What an export contains

The account's own records — the data it created or that was derived for it:

- the profile and saved searches;
- opportunities it matched, the match evaluations and eligibility results;
- generated documents (resumes, cover letters) and their rendered versions' metadata;
- applications, decisions and their event history;
- chat conversations, interview sessions and their transcripts;
- career outcomes, recommendations and strategy history;
- the account's billing subscription state and its usage-metering ledger.
### What an export never contains (§68)

An export is the account's data, not the platform's secrets. It never carries a value
that would compromise the account or the deployment if the archive leaked:

- the password hash (Argon2id) — nor any form of the password;
- session or CSRF token digests;
- the encrypted LLM-connection API-key ciphertext, or any provider credential in
  plaintext — an export surfaces only `has_api_key`, never the key;
- billing webhook secrets, Stripe keys or database credentials — these are deployment
  secrets that never live in the database at all (docs/CREDENTIAL_SECURITY.md).

Shared data the account only *referenced* rather than owns — a company record, a raw
posting — is out of scope: an export copies what belongs to the account, and those rows
belong to no single account.

## Deletion — permanent erasure (§26–29, §69)

`POST /me/deletion` permanently erases the account and everything it owns. It is the
destructive counterpart to the export, and irreversible, so it is gated twice.

### Re-authentication first

A live session is not enough to delete an account. The request carries the account's
current **password**, and the service verifies it against the stored Argon2 hash
*before touching anything*. A wrong password — or a session whose account no longer
exists — raises `ReauthenticationRequired`, mapped to **403 `reauthentication_required`**,
and nothing is touched: not a row, not a stored byte, not a session. A stolen-but-idle
session, or a CSRF that slipped both the cross-site guard and the cookie, cannot erase
an account without also knowing the password.
### What deletion removes

On the correct password, in order:

1. the account's **stored artifacts** are deleted from their object stores — rendered
   document PDFs and export archives — because no foreign key reaches an object store,
   so the bytes must be removed explicitly (§69);
2. **every session** is revoked;
3. the **user row** is deleted last, and the database's own `ON DELETE CASCADE` removes
   every user-owned child row in one statement (profile, searches, matches, documents,
   applications, chat, interviews, career records, subscription, usage ledger, …).

Committing at the request boundary makes a retry idempotent: a re-run after the row is
already gone deletes nothing and reports it, rather than erroring.

The route returns a **receipt** — the counts of what was removed (sessions revoked,
document artifacts deleted, export archives deleted) and the instant it happened — and
never any data about the account that no longer exists. Then it clears the session
cookies, exactly like logout: the browser is signed out of an account that is gone, and
the just-revoked token is refused on replay.

### What deletion deliberately keeps

- **A de-identified billing receipt.** `subscription_events.user_id` is
  `ON DELETE SET NULL`, not cascade: the processed-webhook audit record survives the
  account so a redelivered Stripe event stays a no-op, but it no longer names anyone.
  This is the one row that outlives the account, and it identifies no person.
- **Shared data the account only referenced** — a company, a posting — is untouched.
  Deleting an account does not delete an `Opportunity`; it was never the account's to
  delete.

Deletion also never touches an `ApplicationPolicy`'s safety rules or any eligibility
brake as a side effect — losing an account is not a path to widening a guard.

## Retention — sweeping temporary data on a schedule (§30–31, §70)

Export and deletion are things an *account holder* does to their own data. Retention is
the counterweight the *platform* runs on its own: the data that is temporary **by
design** must not linger past the window it was promised, whoever owns it. A liability
lurks in the opposite of deletion — a platform that keeps everything forever. The sweep
removes exactly three categories, and nothing else:

- **Expired user sessions** — a session past its stored absolute `expires_at` is an
  attack surface no one will resume.
- **Lapsed `READY` export archives** — an export past its own `expires_at` is a copy of
  a user's data still sitting in the object store past the window the download was
  offered for. The sweep purges the bytes and marks the row `EXPIRED`.
- **Idle provider sessions** — an LLM provider handle carries no stored expiry, so
  staleness is idle-based: a row untouched since `now − provider_session_max_idle` is
  state no conversation will resume.

`python -m backend.app.cli.run_retention` runs one sweep under the deployment's
`RetentionSettings` (the provider idle window and a per-statement batch cap; sessions and
exports carry their own expiry, so the policy stays narrow). `--dry-run` reports what a
sweep *would* remove — counted from the same predicates — without touching a row or a
byte. An operator runs it directly until a worker schedules it (M8).

### What retention never auto-expires

The sweep touches only data temporary by the policy's own definition. It holds **no
repository** for the account's persistent history, so there is no path from a sweep to
it — it cannot widen to reach:

- **candidate evidence** and the profile claims the truth guard rests on;
- **application audit history** — applications, decisions and their event trail;
- **career outcomes, recommendations and strategy history**;
- **user-created documents** (resumes, cover letters) and their rendered versions.

A `PENDING` or `FAILED` export is likewise persistent record, not a lapsed archive: the
export sweep purges only lapsed `READY` rows, so a request not yet produced and an
auditable failed production both survive.

### Idempotent and fail-safe (§31)

A rerun — after a crash, or a redelivered schedule — reaches the same state and raises
nothing: deleting an already-gone row or archive is a no-op, and an export drops out of
the work-list the instant its row becomes `EXPIRED`. Crucially, an export purge marks the
row `EXPIRED` **only after** the store confirms the bytes are gone. If the store faults,
the row is left `READY` and the fault is recorded as a typed failure that names the export
and a fixed reason — never its contents — so a rerun retries rather than orphaning bytes
behind an `EXPIRED` row. The CLI exits `0` on a clean sweep and `3` when a purge failed.

## Where each guarantee is proved

| Guarantee | Proved in |
| --- | --- |
| Export/deletion owner is the session, never the body (§63) | `tests/test_v2_account_export.py`, `tests/test_v2_account_deletion.py` (route flows) |
| Export omits every secret (§68) | `tests/test_v2_account_export.py` |
| Re-authentication gates deletion; wrong password touches nothing | `tests/test_v2_account_deletion.py` (service + route) |
| Stored bytes are removed, not just dereferenced (§69) | `tests/test_v2_account_deletion.py` (service over real local stores) |
| The database cascade removes every user-owned row | `tests/test_v2_persistence_constraints.py` |
| `subscription_events` is de-identified, not deleted | `tests/test_v2_persistence_constraints.py` |
| The two deletion repository primitives (`delete`, artifact-key enumeration) | `tests/test_v2_persistence_repositories.py` |
| Retention sweeps only expired sessions, lapsed `READY` archives and idle provider sessions; persistent history is never touched (§30, §70) | `tests/test_v2_retention.py` |
| Retention is idempotent and fail-safe — a store fault leaves the row `READY` for a rerun (§31) | `tests/test_v2_retention.py` |
| The retention repository primitives (`count_expired`, `list_expired`, `delete_stale`/`count_stale`) match across owners | `tests/test_v2_persistence_repositories.py`, `tests/test_v2_persistence_llm.py` |
