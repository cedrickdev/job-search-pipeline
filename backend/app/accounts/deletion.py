"""AccountDeletionService: erase an account and everything it owns (§26-29).

The destructive counterpart to the export M5 shipped. Where an export *copies* an account's
data out, deletion *removes* it — every user-owned row, every stored artifact, every live
session — and leaves behind only what a receipt needs to prove it happened and what the law
lets a business keep: a de-identified billing record that no longer names anyone.

Three promises hold this together:

**Re-authenticate before deleting a byte.** Deletion is irreversible, so a valid session is not
enough: the caller must also present the account's password, which the service verifies against
the stored hash. A stolen-but-idle session, or a CSRF that slipped both guards, cannot erase an
account without also knowing the password. A wrong password raises `ReauthenticationRequired`
and *nothing* is touched — the check runs first, before any read or write.

**Rows cascade; bytes do not.** Every user-owned table declares `ON DELETE CASCADE` from
`users.id`, so a single `users.delete` removes the profile, searches, documents, applications,
conversations, interviews, career records, subscription, usage and export *rows* in one
statement — and `subscription_events` alone declares `ON DELETE SET NULL`, so a billing receipt
survives the account it can no longer name (§28). But a rendered document's PDF and an export's
archive live in a *store*, not a row, so no foreign key reaches them: the service enumerates
their keys and deletes them explicitly, before the row that named them is gone.

**The owner comes from the session, never the body.** `delete` takes the `user_id` the API
resolved from the session cookie and scopes every collaborator to it. There is no field a caller
could set to erase another account, and the route never reads an id from the request body (§63).

Order is deliberate: gather the artifact keys while the rows still exist, delete the artifacts
and revoke the sessions, then delete the account row last. Because the request commits once at
its boundary, any failure before that final commit rolls the database work back, and every step
is idempotent — so a retried deletion after a partial failure converges on the same erased
state rather than compounding it.
"""
from dataclasses import dataclass
from datetime import datetime

from pydantic import SecretStr

from backend.app.core.passwords import verify_password
from backend.app.documents.artifacts import DocumentArtifactStore
from backend.app.domain.identifiers import UserId
from backend.app.exports.store import AccountExportStore
from backend.app.repositories.contracts import (
    CandidateDocumentRepository,
    SessionRepository,
    UserRepository,
)


class ReauthenticationRequired(Exception):
    """The password confirming a destructive deletion was missing or wrong.

    Deliberately singular: whether the account could not be loaded or the password simply did
    not match, the caller learns only that it must re-authenticate — telling the two apart would
    turn this endpoint into an oracle for whether a session's account still exists. The API maps
    it to a 403, and the service raises it before any row or byte is touched, so a refused
    deletion changes nothing.
    """


@dataclass(frozen=True)
class AccountDeletionReceipt:
    """What a completed deletion returns: counts and an instant, never data (§27-29).

    Enough for a client to confirm the erasure — how many sessions were revoked, how many stored
    artifacts were removed, and when it happened — without echoing a single fact about the
    account that no longer exists. Not persisted: the account it describes is gone, so the receipt
    lives only as long as the response that carries it.
    """

    user_id: UserId
    deleted_at: datetime
    sessions_revoked: int
    document_artifacts_removed: int
    export_archives_removed: int


class AccountDeletionService:
    """Re-authenticate, then erase an account and everything it owns (§26-29).

    Every collaborator is handed in and scoped by `user_id` on each call: the user repository
    (load-and-verify, then the cascading delete), the session repository (revoke-all), the
    document repository (enumerate artifact keys), and the two artifact stores (delete the bytes
    the cascade cannot reach). The service holds no clock and no owner of its own — `now` and
    `user_id` are passed on every call — so what it deletes and when are fully determined by its
    inputs.
    """

    def __init__(self, *,
                 users: UserRepository,
                 sessions: SessionRepository,
                 documents: CandidateDocumentRepository,
                 document_store: DocumentArtifactStore,
                 export_store: AccountExportStore) -> None:
        self._users = users
        self._sessions = sessions
        self._documents = documents
        self._document_store = document_store
        self._export_store = export_store

    async def delete(self, user_id: UserId, *,
                     password: SecretStr, now: datetime) -> AccountDeletionReceipt:
        """Erase this account and everything it owns, returning a receipt (§26-29).

        Verifies the password against the stored hash first — a mismatch, or an account that no
        longer exists, raises `ReauthenticationRequired` and touches nothing. On success it
        gathers the artifact keys while the rows still exist, deletes the document artifacts and
        export archives from their stores, revokes every live session, and finally deletes the
        account row so the database cascade removes every user-owned row and de-identifies the
        billing receipt. The counts on the returned receipt reflect what this call removed.
        """
        user = await self._users.get(user_id)
        if user is None or not verify_password(password, user.password_hash).matched:
            raise ReauthenticationRequired

        # Gather the keys before the rows are gone: once `users.delete` cascades, the document
        # rows that name these artifacts no longer exist to be enumerated.
        artifact_keys = await self._documents.artifact_storage_keys_for_user(user_id)
        document_artifacts_removed = sum(
            1 for key in artifact_keys if self._document_store.delete(key))
        export_archives_removed = self._export_store.delete_all_for_user(user_id)
        sessions_revoked = await self._sessions.revoke_all_for_user(user_id, now)
        await self._users.delete(user_id)

        return AccountDeletionReceipt(
            user_id=user_id,
            deleted_at=now,
            sessions_revoked=sessions_revoked,
            document_artifacts_removed=document_artifacts_removed,
            export_archives_removed=export_archives_removed,
        )
