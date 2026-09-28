"""`/api/v2/me/exports`: a portable, secret-free copy of one account's own data (§23-25).

One surface, one authorization model. The owner is never in the path — it comes from the
session — so a request can neither produce nor read an export belonging to another account
(docs/ENGINEERING_STANDARDS.md §Security, §23). `POST /me/exports` requests an export and
produces it, `GET /me/exports` lists this account's export history, `GET /me/exports/{id}`
polls one, and `GET /me/exports/{id}/download` streams the produced archive.

The reads answer 404 for "no such export" and "not yours" alike, so a caller cannot learn
another account holds an export by asking for its id. A download of an export that is not
`READY`-and-unexpired is a 409, not a 404: the resource is real and the state is either
temporary (poll it) or terminal for a reason the export's own status explains. What an export
*contains* and how it excludes secrets is the gatherer's and service's job (§24); this module
only turns the session and the request clock into service calls.
"""
from fastapi import APIRouter, Response, status

from backend.app.api.dependencies import AccountExports, CurrentSession, Now
from backend.app.api.schemas import (
    AccountExportListResponse,
    AccountExportResponse,
)
from backend.app.domain.identifiers import AccountExportId
from backend.app.exports import ACCOUNT_EXPORT_MEDIA_TYPE

router = APIRouter(tags=["v2-account-export"])


@router.post("/me/exports", response_model=AccountExportResponse,
             status_code=status.HTTP_201_CREATED)
async def create_export(current: CurrentSession, service: AccountExports,
                        instant: Now) -> AccountExportResponse:
    """Request an export of everything this account holds, and produce it now (§23-25).

    201, because it creates a resource: each request is its own export — asking twice is two
    exports, not an idempotent replace. The synchronous path produces the archive before
    answering, so the response is the finished `READY` export (or a `FAILED` one carrying a
    machine reason if production could not complete); a client polls neither in the common case.
    The archive is gathered from this account's own rows only and swept for secrets before a byte
    is stored (§24).
    """
    export = await service.create(current.user.id, now=instant)
    return AccountExportResponse.of(export, now=instant)


@router.get("/me/exports", response_model=AccountExportListResponse)
async def list_exports(current: CurrentSession, service: AccountExports,
                       instant: Now) -> AccountExportListResponse:
    """This account's export requests, most recently updated first (§23).

    `is_downloadable` on each is computed against the request clock, so an archive whose
    retention window has lapsed reads as not downloadable even before the retention sweep flips
    its status to `EXPIRED`.
    """
    exports = await service.list(current.user.id)
    return AccountExportListResponse.of(exports, now=instant)


@router.get("/me/exports/{export_id}", response_model=AccountExportResponse)
async def read_export(export_id: AccountExportId, current: CurrentSession,
                      service: AccountExports, instant: Now) -> AccountExportResponse:
    """One export's lifecycle record (§23).

    404 for "no such export" and "not yours" alike: the service raises one error for both, so a
    caller cannot enumerate another account's exports by id.
    """
    export = await service.get(current.user.id, export_id)
    return AccountExportResponse.of(export, now=instant)


@router.get("/me/exports/{export_id}/download")
async def download_export(export_id: AccountExportId, current: CurrentSession,
                          service: AccountExports, instant: Now) -> Response:
    """Stream the produced archive of a `READY`, unexpired export (§25).

    A binary response, not JSON envelope: the bytes are read from the export store and returned
    with the archive's media type and a `Content-Disposition` naming the file. 404 when the
    export is not this account's; 409 (`account_export_not_ready`) when it exists but has no
    downloadable archive right now — it is still `PENDING`, it `FAILED`, or its window has lapsed.
    The download availability is re-checked server-side against the request clock, so an archive
    past its retention window is refused even before a sweep has marked it `EXPIRED`.
    """
    stored = await service.download(current.user.id, export_id, now=instant)
    return Response(
        content=stored.content, media_type=ACCOUNT_EXPORT_MEDIA_TYPE,
        headers={"Content-Disposition":
                 f'attachment; filename="account-export-{export_id}.json"'})
