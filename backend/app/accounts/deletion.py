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
conversations, interviews, career records, subscription, usage, queued tasks and export *rows* in
one statement — and `subscription_events` alone declares `ON DELETE SET NULL`, so a billing
receipt survives the account it can no longer name (§28). But a rendered document's PDF and an
export's archive live in a *store*, not a row, so no foreign key reaches them: the service
enumerates their keys and deletes them explicitly, before the row that named them is gone.

**A paid external subscription is settled before the local row is dropped (§27).** A subscription
lives in two places — a local row *and* the billing provider's own ledger — and the cascade only
reaches the first. Deleting the local row while the provider keeps charging would strand an
account paying for a service it can no longer reach, so deletion cancels the provider-side
subscription first, through the provider-neutral `BillingProvider` port (never a Stripe call here).
The cancel is idempotent and runs before any irreversible byte-deletion; if the provider cannot
confirm it, the whole request fails closed — nothing is deleted and the account stays intact —
rather than orphaning a live subscription.

**The owner comes from the session, never the body.** `delete` takes the `user_id` the API
resolved from the session cookie and scopes every collaborator to it. There is no field a caller
could set to erase another account, and the route never reads an id from the request body (§63).

Order is deliberate and follows §27: re-authenticate, revoke every session (so no new work can be
started under this account), cancel the provider-side subscription, cancel the account's still-
queued tasks, gather and delete the stored artifacts, then delete the account row last so the
cascade removes every user-owned row. Because the request commits once at its boundary, any
failure before that final commit rolls the database work back, and every step is idempotent — so
a retried deletion after a partial failure converges on the same erased state rather than
compounding it.
"""
from dataclasses import dataclass
from datetime import datetime

from pydantic import SecretStr

from backend.app.billing.provider import BillingProvider
from backend.app.core.passwords import verify_password
from backend.app.documents.artifacts import DocumentArtifactStore
from backend.app.domain.identifiers import UserId
from backend.app.exports.store import AccountExportStore
from backend.app.repositories.contracts import (
    CandidateDocumentRepository,
    SessionRepository,
    SubscriptionRepository,
    TaskRunRepository,
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

    Enough for a client to confirm the erasure — how many sessions were revoked, whether the
    provider-side subscription was canceled, how many queued tasks were dropped, how many stored
    artifacts were removed, and when it happened — without echoing a single fact about the account
    that no longer exists. Not persisted: the account it describes is gone, so the receipt lives
    only as long as the response that carries it.
    """

    user_id: UserId
    deleted_at: datetime
    sessions_revoked: int
    external_subscription_canceled: bool
    tasks_canceled: int
    document_artifacts_removed: int
    export_archives_removed: int


class AccountDeletionService:
    """Re-authenticate, then erase an account and everything it owns (§26-29).

    Every collaborator is handed in and scoped by `user_id` on each call: the user repository
    (load-and-verify, then the cascading delete), the session repository (revoke-all), the
    subscription repository and billing provider (settle the provider-side subscription), the task
    repository (cancel queued work), the document repository (enumerate artifact keys), and the two
    artifact stores (delete the bytes the cascade cannot reach). The service holds no clock and no
    owner of its own — `now` and `user_id` are passed on every call — so what it deletes and when
    are fully determined by its inputs.
    """

    def __init__(self, *,
                 users: UserRepository,
                 sessions: SessionRepository,
                 subscriptions: SubscriptionRepository,
                 tasks: TaskRunRepository,
                 provider: BillingProvider,
                 documents: CandidateDocumentRepository,
                 document_store: DocumentArtifactStore,
                 export_store: AccountExportStore) -> None:
        self._users = users
        self._sessions = sessions
        self._subscriptions = subscriptions
        self._tasks = tasks
        self._provider = provider
        self._documents = documents
        self._document_store = document_store
        self._export_store = export_store

    async def delete(self, user_id: UserId, *,
                     password: SecretStr, now: datetime) -> AccountDeletionReceipt:
        """Erase this account and everything it owns, returning a receipt (§26-29).

        Verifies the password against the stored hash first — a mismatch, or an account that no
        longer exists, raises `ReauthenticationRequired` and touches nothing. On success it follows
        §27's order: revoke every session (so no new work starts under the account), cancel the
        provider-side subscription when one is provider-backed (idempotently, and before any
        irreversible byte-deletion, so a provider that cannot confirm fails the whole request
        closed rather than orphaning a live subscription), cancel the account's still-queued tasks,
        gather the artifact keys while the rows still exist, delete the document artifacts and
        export archives from their stores, and finally delete the account row so the cascade removes
        every user-owned row and de-identifies the billing receipt. The counts on the returned
        receipt reflect what this call removed.
        """
        user = await self._users.get(user_id)
        if user is None or not verify_password(password, user.password_hash).matched:
            raise ReauthenticationRequired

        # Revoke sessions first: a deleted account must not be able to start new work mid-deletion.
        sessions_revoked = await self._sessions.revoke_all_for_user(user_id, now)

        # Settle the provider-side subscription before anything irreversible. If the provider
        # cannot confirm the cancel it raises PROVIDER_UNAVAILABLE, which propagates and rolls the
        # whole request back — nothing deleted — so the account is never dropped locally while a
        # paid subscription keeps charging. Idempotent, so a retried deletion converges.
        external_subscription_canceled = await self._cancel_external_subscription(user_id)

        # Cancel queued tasks (a DELETE that locks the rows now, closing the lease-race window);
        # the cascade would remove them anyway, but not before a worker could claim one.
        tasks_canceled = await self._tasks.cancel_queued_for_user(user_id)

        # Gather the keys before the rows are gone: once `users.delete` cascades, the document
        # rows that name these artifacts no longer exist to be enumerated.
        artifact_keys = await self._documents.artifact_storage_keys_for_user(user_id)
        document_artifacts_removed = sum(
            1 for key in artifact_keys if self._document_store.delete(key))
        export_archives_removed = self._export_store.delete_all_for_user(user_id)
        await self._users.delete(user_id)

        return AccountDeletionReceipt(
            user_id=user_id,
            deleted_at=now,
            sessions_revoked=sessions_revoked,
            external_subscription_canceled=external_subscription_canceled,
            tasks_canceled=tasks_canceled,
            document_artifacts_removed=document_artifacts_removed,
            export_archives_removed=export_archives_removed,
        )

    async def _cancel_external_subscription(self, user_id: UserId) -> bool:
        """Cancel the account's live provider-side subscription, if any; return whether it did.

        The live commercial relationship is `get_current` (the most-recently-updated non-CANCELED
        subscription). Only a provider-backed one carrying an external handle can be canceled with
        the provider — the internal free tier has nothing external to settle — so any other case is
        a `False` no-op. A provider that cannot confirm raises `BillingError(PROVIDER_UNAVAILABLE)`,
        which propagates so the caller fails closed.
        """
        subscription = await self._subscriptions.get_current(user_id)
        if (subscription is None
                or not subscription.is_provider_backed
                or subscription.external_subscription_id is None):
            return False
        await self._provider.cancel_subscription(
            external_subscription_id=subscription.external_subscription_id)
        return True
