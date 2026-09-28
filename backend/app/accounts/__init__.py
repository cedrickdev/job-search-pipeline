"""Account-lifecycle services that span an account's whole footprint (§26-29).

Where `backend.app.services` holds services scoped to one aggregate, this package holds the ones
that act on an *account* as a whole — today, its permanent deletion. The public surface is the
deletion service, its receipt, and the re-authentication error the API maps to a 403.
"""
from backend.app.accounts.deletion import (
    AccountDeletionReceipt,
    AccountDeletionService,
    ReauthenticationRequired,
)

__all__ = [
    "AccountDeletionReceipt",
    "AccountDeletionService",
    "ReauthenticationRequired",
]
