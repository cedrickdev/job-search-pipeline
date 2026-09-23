"""CandidateEvidenceService: adding the evidence and claims a document rests on.

The evidence store is what Phase 10's truth guarantee stands on: a document may
only cite `CandidateEvidence` the profile holds, and a `CandidateClaim` may only
rest on the same. Before a candidate can have a résumé generated, then, the store
has to be filled — and this service is the write side of that, kept apart from
`OnboardingService` for one reason worth stating: evidence and claims are the
candidate's *attested record*, added and grown over time, while onboarding is the
profile's *identity and preferences*, saved as a whole. Folding the two together
would make a profile edit and an evidence addition the same call, and a form that
re-saved the profile would then have to carry every evidence record or drop it.

Two writes, both onto the account's own profile:

1. `add_evidence` — record one attested fact (a CV bullet, a diploma, a permit
   document), under a fresh id and this account's ownership;
2. `add_claim` — assert something the platform may say on the candidate's behalf,
   citing evidence the profile already holds. A claim that cites evidence the
   profile does not hold is refused with a typed error, not a 500 — the aggregate
   would reject it anyway (`_claims_rest_on_held_evidence`), and this turns that
   invariant into a client-legible message.

**The owner comes from the session, never the body.** Each method takes `user_id`,
reads the account's profile through it, and stamps every new record with it, so a
request cannot file evidence under someone else's profile or claim ownership it
does not have (docs/ENGINEERING_STANDARDS.md §Security).
"""
from dataclasses import dataclass
from datetime import date, datetime

from backend.app.domain.candidate import (
    CandidateClaim,
    CandidateEvidence,
    CandidateProfile,
    ClaimType,
    EvidenceKind,
    EvidenceProvenance,
)
from backend.app.domain.identifiers import (
    EvidenceId,
    UserId,
    new_claim_id,
    new_evidence_id,
)
from backend.app.repositories.contracts import CandidateProfileRepository
from backend.app.services.assessment import CandidateProfileNotFound


class ClaimCitesUnknownEvidence(Exception):
    """A claim cited evidence the profile does not hold.

    The service form of the aggregate's `_claims_rest_on_held_evidence` invariant:
    a claim resting on an id no evidence record carries is a fabricated claim, and
    it is refused. The API maps it to a 422 naming the ids that were not found, so
    a client fixes the citation rather than reading a 500.
    """

    def __init__(self, evidence_ids: tuple[EvidenceId, ...]) -> None:
        super().__init__(
            "claim cites evidence absent from the profile: "
            + ", ".join(str(eid) for eid in evidence_ids))
        self.evidence_ids = evidence_ids


@dataclass(frozen=True, slots=True)
class EvidenceDraft:
    """One evidence record as submitted: no id, no owner, no recorded_at.

    A plain dataclass rather than a `DomainModel`, because it is assembled by the
    API schema (which validates the fields) and consumed here; the values it
    carries are the domain's own, and the service turns it into a
    `CandidateEvidence` under a fresh id and the session's owner.
    """

    kind: EvidenceKind
    provenance: EvidenceProvenance
    summary: str
    reference_key: str | None = None
    detail: str | None = None
    issued_on: date | None = None
    valid_until: date | None = None
    source_document: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimDraft:
    """One claim as submitted: no id, no owner, but the evidence ids it cites."""

    claim_type: ClaimType
    label: str
    evidence_ids: tuple[EvidenceId, ...]
    detail: str | None = None


class CandidateEvidenceService:
    """Append evidence and claims to the account's candidate profile.

    Holds only the profile repository: evidence and claims are child collections of
    the profile aggregate, reconciled by its upsert, so there is nothing else to
    write. The repository is handed in, so the request layer wires it over
    PostgreSQL and a flow test over the fake, and this class never learns which.
    """

    def __init__(self, profiles: CandidateProfileRepository) -> None:
        self._profiles = profiles

    async def add_evidence(self, user_id: UserId, draft: EvidenceDraft, *,
                           now: datetime) -> tuple[CandidateProfile, CandidateEvidence]:
        """Record one attested fact on the account's profile.

        Returns the saved profile and the new record, so a caller can echo the id
        the record was filed under — the id a later claim will cite. Raises
        `CandidateProfileNotFound` when onboarding has not saved a profile yet:
        evidence hangs off a profile, and there is nothing to hang it on.
        """
        profile = await self._require_profile(user_id)
        evidence = CandidateEvidence(
            id=new_evidence_id(), user_id=user_id, kind=draft.kind,
            provenance=draft.provenance, reference_key=draft.reference_key,
            summary=draft.summary, detail=draft.detail, issued_on=draft.issued_on,
            valid_until=draft.valid_until, source_document=draft.source_document,
            recorded_at=now)
        saved = await self._profiles.upsert(profile.model_copy(update={
            "evidence": profile.evidence + (evidence,), "updated_at": now}))
        return saved, evidence

    async def add_claim(self, user_id: UserId, draft: ClaimDraft, *,
                        now: datetime) -> tuple[CandidateProfile, CandidateClaim]:
        """Assert one claim, citing evidence the profile already holds.

        The citation is checked here, before the write, so an unknown-evidence
        citation is a typed `ClaimCitesUnknownEvidence` a client can act on rather
        than the aggregate's `ValueError` surfacing as a 500. The aggregate re-checks
        it on construction regardless — this service cannot be the only guard — but
        catching it early is what gives the useful message.
        """
        profile = await self._require_profile(user_id)
        held = {item.id for item in profile.evidence}
        missing = tuple(eid for eid in draft.evidence_ids if eid not in held)
        if missing:
            raise ClaimCitesUnknownEvidence(missing)
        claim = CandidateClaim(
            id=new_claim_id(), user_id=user_id, claim_type=draft.claim_type,
            label=draft.label, detail=draft.detail,
            evidence_ids=draft.evidence_ids)
        saved = await self._profiles.upsert(profile.model_copy(update={
            "claims": profile.claims + (claim,), "updated_at": now}))
        return saved, claim

    async def _require_profile(self, user_id: UserId) -> CandidateProfile:
        profile = await self._profiles.get_default(user_id)
        if profile is None:
            raise CandidateProfileNotFound(str(user_id))
        return profile
