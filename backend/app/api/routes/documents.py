"""`/api/v2`: the candidate evidence store and the generated documents.

Two surfaces, one authorization model. The owner is never in the path — it comes
from the session — so a request cannot file evidence under, or read a document
belonging to, another account (docs/ENGINEERING_STANDARDS.md §Security).

The evidence surface lives under `/me`, beside the profile it hangs from:
`POST /me/evidence` records an attested fact, `POST /me/claims` asserts something
resting on evidence already held, and `GET /me/evidence` returns the whole
attested record. These are the writes that fill the store a document is built
from, kept apart from the profile form for the reason `CandidateEvidenceService`
explains.

The document surface is keyed by the posting. `POST /opportunities/{id}/resume`
and `.../cover-letter` generate a version — a `POST` because each call is a new
attempt that grows the history, not an idempotent replace. `GET /documents` and
`GET /documents/{id}` read; `GET /documents/{id}/download` streams the newest
rendered PDF. The reads answer 404 for "no such document" and "not yours" alike,
so a caller cannot learn another account holds a document by asking for it.
"""
from fastapi import APIRouter, Response, status

from backend.app.api.dependencies import CurrentSession, Documents, Evidence, Now
from backend.app.api.schemas import (
    AddClaimRequest,
    AddEvidenceRequest,
    CandidateClaimResponse,
    CandidateDocumentListResponse,
    CandidateDocumentResponse,
    CandidateEvidenceListResponse,
    CandidateEvidenceResponse,
    GenerateDocumentRequest,
)
from backend.app.domain.documents import CandidateDocumentType
from backend.app.domain.identifiers import CandidateDocumentId, OpportunityId
from backend.app.services.evidence import ClaimDraft, EvidenceDraft

router = APIRouter(tags=["v2-documents"])


# --- candidate evidence store ------------------------------------------------

@router.post("/me/evidence", response_model=CandidateEvidenceResponse,
             status_code=status.HTTP_201_CREATED)
async def add_evidence(body: AddEvidenceRequest, current: CurrentSession,
                       service: Evidence, instant: Now) -> CandidateEvidenceResponse:
    """Record one attested fact on the account's profile.

    201, because it creates a resource: the response carries the id the record was
    filed under, which a later claim or a generated document line cites. 404 when
    onboarding has not saved a profile yet — evidence hangs off a profile, and
    there is nothing to hang it on.
    """
    _, evidence = await service.add_evidence(
        current.user.id,
        EvidenceDraft(
            kind=body.kind, provenance=body.provenance, summary=body.summary,
            reference_key=body.reference_key, detail=body.detail,
            issued_on=body.issued_on, valid_until=body.valid_until,
            source_document=body.source_document),
        now=instant)
    return CandidateEvidenceResponse.of(evidence)


@router.post("/me/claims", response_model=CandidateClaimResponse,
             status_code=status.HTTP_201_CREATED)
async def add_claim(body: AddClaimRequest, current: CurrentSession,
                    service: Evidence, instant: Now) -> CandidateClaimResponse:
    """Assert one claim, citing evidence the profile already holds.

    422 when a cited id names no evidence record on the profile: the service
    refuses it rather than writing a claim that rests on nothing, and the error
    names the ids that were not found so a client fixes the citation.
    """
    _, claim = await service.add_claim(
        current.user.id,
        ClaimDraft(claim_type=body.claim_type, label=body.label,
                   evidence_ids=body.evidence_ids, detail=body.detail),
        now=instant)
    return CandidateClaimResponse.of(claim)


@router.get("/me/evidence", response_model=CandidateEvidenceListResponse)
async def list_evidence(current: CurrentSession,
                        service: Documents) -> CandidateEvidenceListResponse:
    """The account's whole attested record: its evidence and its claims.

    Read through the document service's profile view so one dependency answers the
    profile-backed reads. 404 when onboarding has not saved a profile — the same
    state `GET /me/profile` reports, so a client sends the user to onboarding.
    """
    profile = await service.candidate_profile(current.user.id)
    return CandidateEvidenceListResponse.of(profile)


# --- generated documents -----------------------------------------------------

@router.post("/opportunities/{opportunity_id}/resume",
             response_model=CandidateDocumentResponse)
async def generate_resume(opportunity_id: OpportunityId,
                          body: GenerateDocumentRequest, current: CurrentSession,
                          service: Documents, instant: Now
                          ) -> CandidateDocumentResponse:
    """Generate an ATS résumé for a posting, from this account's evidence.

    Appends a version to the résumé document for the `(profile, opportunity,
    RESUME)` triple, returning the whole document so a client sees the new attempt
    in its history. 404 when the account has no profile or no such posting; 409
    (`insufficient_evidence`) when the profile carries too little evidence to build
    a truthful résumé — the honest answer is to say so, not to invent content.
    """
    document = await service.generate(
        current.user.id, opportunity_id, CandidateDocumentType.RESUME,
        now=instant, language=body.language)
    return CandidateDocumentResponse.of(document)


@router.post("/opportunities/{opportunity_id}/cover-letter",
             response_model=CandidateDocumentResponse)
async def generate_cover_letter(opportunity_id: OpportunityId,
                                body: GenerateDocumentRequest, current: CurrentSession,
                                service: Documents, instant: Now
                                ) -> CandidateDocumentResponse:
    """Generate a cover letter for a posting, from this account's evidence.

    The persuasive counterpart to the résumé, versioned the same way and subject to
    the same guard: every body paragraph cites the candidate evidence it draws on.
    Same error codes as the résumé endpoint.
    """
    document = await service.generate(
        current.user.id, opportunity_id, CandidateDocumentType.COVER_LETTER,
        now=instant, language=body.language)
    return CandidateDocumentResponse.of(document)


@router.get("/documents", response_model=CandidateDocumentListResponse)
async def list_documents(current: CurrentSession,
                         service: Documents) -> CandidateDocumentListResponse:
    """This account's documents, most recently updated first."""
    documents = await service.documents(current.user.id)
    return CandidateDocumentListResponse.of(documents)


@router.get("/documents/{document_id}", response_model=CandidateDocumentResponse)
async def read_document(document_id: CandidateDocumentId, current: CurrentSession,
                        service: Documents) -> CandidateDocumentResponse:
    """One document with its whole version history.

    404 for "no such document" and "not yours" alike: the service raises one error
    for both, so a caller cannot enumerate another account's documents by id.
    """
    document = await service.document(current.user.id, document_id)
    return CandidateDocumentResponse.of(document)


@router.get("/documents/{document_id}/download")
async def download_document(document_id: CandidateDocumentId, current: CurrentSession,
                            service: Documents) -> Response:
    """Stream the newest rendered PDF of a document.

    A binary response, not JSON: the bytes are read from the artifact store and
    returned with the stored media type and a `Content-Disposition` naming the
    file. 404 when the document is not this account's; 409
    (`document_not_rendered`) when it exists but no version has cleared the guard
    and been rendered yet — the resource is real, the state is temporary.
    """
    stored = await service.download(current.user.id, document_id)
    return Response(
        content=stored.content, media_type=stored.media_type,
        headers={"Content-Disposition":
                 f'attachment; filename="{document_id}.pdf"'})
