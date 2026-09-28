"""`/api/v2/me/deletion`: permanently erase an account and everything it owns (§26-29).

The destructive counterpart to `me/exports`. One route, one authorization model: the owner is
the session's, never the path or the body, so a request can only ever erase its own account
(docs/ENGINEERING_STANDARDS.md §Security, §63).

Because deletion is irreversible, a live session is not enough — the request carries the
account's password, which the service verifies against the stored hash before deleting a byte.
A stolen-but-idle session, or a CSRF that slipped both guards, cannot erase an account without
also knowing the password; a wrong password is a 403 (`reauthentication_required`) and nothing
is touched. On success every user-owned row is removed by the database cascade, the account's
stored artifacts are deleted from their stores, every session is revoked, and — like logout —
the session cookies are cleared, so the browser is signed out of an account that no longer
exists. What deletion *removes* and what it deliberately keeps (a de-identified billing receipt)
is the service's job; this module only turns the session, the body and the request clock into a
service call and clears the cookies.
"""
from fastapi import APIRouter, Response

from backend.app.api.cookies import clear_session_cookies
from backend.app.api.dependencies import AccountDeletion, Auth, CurrentSession, Now
from backend.app.api.schemas import AccountDeletionRequest, AccountDeletionResponse

router = APIRouter(tags=["v2-account"])


@router.post("/me/deletion", response_model=AccountDeletionResponse)
async def delete_account(body: AccountDeletionRequest, response: Response,
                         current: CurrentSession, service: AccountDeletion,
                         settings: Auth, instant: Now) -> AccountDeletionResponse:
    """Permanently erase this account and everything it owns, on password confirmation (§26-29).

    200 with a receipt — counts of what was removed and the instant it happened — never any data
    about the account that no longer exists. The password in the body re-authenticates the
    caller; a mismatch raises `ReauthenticationRequired` (mapped to 403) before anything is
    touched. On success the session cookies are cleared, because the session they carry has just
    been revoked along with the account. Shared data the account only referenced (a posting, a
    company) is untouched, and the de-identified billing receipt survives via `ON DELETE SET NULL`.
    """
    receipt = await service.delete(current.user.id, password=body.password, now=instant)
    clear_session_cookies(response, settings=settings)
    return AccountDeletionResponse.of(receipt)
