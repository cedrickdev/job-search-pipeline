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
