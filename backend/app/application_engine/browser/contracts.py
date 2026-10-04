"""The driver contract and the typed values that cross it (§3, §8, §24).

A `BrowserApplicationDriver` is the one shape the browser worker dispatches to. It knows how a
single ATS is structured — where its fields are, what its submit control is, how it confirms — and
nothing about whether an application should be sent (§47). Two phases, the same split the outer
adapter draws (§19): `prepare` navigates, inspects and fingerprints the form and never clicks the
final control; `submit` fills the resolved answers, uploads the pinned documents and clicks the one
irreversible control at most once.

The values here are deliberately structural and secret-free (§24): a `PreparedBrowserForm` reports
the questions a form *asks* and a hash of its structure, never its raw HTML; a
`BrowserSubmissionEvidence` reports whether a control was clicked and whether a positive
confirmation was read, never a page dump, a cookie or an auth header.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from backend.app.application_engine.contracts import EngineValue
from backend.app.domain.application_answer import ApplicationAnswer, ApplicationQuestion
from backend.app.domain.application_channel import HumanRequiredReason
from backend.app.domain.base import HttpUrlStr, NonEmptyStr
from backend.app.domain.documents import CandidateDocumentType

if TYPE_CHECKING:  # pragma: no cover - typing only; Playwright lives behind the worker
    from playwright.async_api import Page


class ResolvedUpload(EngineValue):
    """A pinned document the worker has already verified is safe to upload (§13).

    `path` is a local artifact file the worker confirmed exists for a RENDERED, guard-cleared
    version *before* the driver is called; `filename` is the candidate-facing name the ATS should
    see. The driver only attaches the file at this path — it never resolves "the latest" document
    and never reaches into the document store itself.
    """

    document_type: CandidateDocumentType
    path: NonEmptyStr
    filename: NonEmptyStr


class DriverContext(EngineValue):
    """What a driver is handed for one phase — navigation plus, at submit, the cleared answers.

    Carries no candidate PII a driver could leak on its own: at `prepare` only `target_url` is
    used (the worker builds proposals from the profile afterwards, §9); at `submit` the `answers`
    are already the deterministically resolved `ApplicationAnswer`s (§12) and `uploads` the
    worker-verified pinned files (§13). A driver fills these and invents nothing.
    """

    target_url: HttpUrlStr | None = None
    answers: tuple[ApplicationAnswer, ...] = ()
    uploads: tuple[ResolvedUpload, ...] = ()
    expected_form_fingerprint: NonEmptyStr | None = None
    correlation_id: NonEmptyStr | None = None

    def answer_for(self, key: str) -> str | None:
        """The resolved value cleared to fill the field named `key`, or `None`."""
        for answer in self.answers:
            if answer.question_key == key:
                return answer.value
        return None

    def upload_for(self, document_type: CandidateDocumentType) -> ResolvedUpload | None:
        """The verified pinned upload for a document type, or `None` if none was pinned."""
        for upload in self.uploads:
            if upload.document_type is document_type:
                return upload
        return None


class PreparedBrowserForm(EngineValue):
    """What a driver's `prepare` read from the form — structure, never raw HTML (§8, §24).

    `questions` are the fields the form asks, each with a stable key (§9); `form_fingerprint` is a
    hash of the normalized structure (§14) for the submit-time re-check; `human_required_reason` is
    set when preparation hit a hard stop only a person can clear — a login wall, a CAPTCHA, an MFA
    prompt, an unrecognized form — and when it is set the form was not safely readable and must not
    be filled. `detail` is a composed, secret-free sentence (§33).
    """

    platform: NonEmptyStr
    questions: tuple[ApplicationQuestion, ...] = ()
    form_fingerprint: NonEmptyStr | None = None
    human_required_reason: HumanRequiredReason | None = None
    detail: NonEmptyStr | None = None

    @property
    def blocked(self) -> bool:
        return self.human_required_reason is not None


class BrowserSubmissionEvidence(EngineValue):
    """What a driver's `submit` observed — clicked-or-not, confirmed-or-not (§20-23).

    The four cases the orchestrator maps upward: a pre-click stop only a human can clear sets
    `human_required_reason` with `clicked=False`; a pre-click failure that is safe to retry sets
    neither `clicked` nor a reason; a click with a positive confirmation sets `clicked=True`,
    `confirmed=True` and, when the ATS returned one, a `confirmation_reference`; a click with no
    readable confirmation sets `clicked=True`, `confirmed=False` — the STATE_UNKNOWN that must
    never be retried (§22). `confirmed` requires positive evidence, never the absence of an error
    (§21). `detail` is composed and secret-free (§33).
    """

    clicked: bool = False
    confirmed: bool = False
    confirmation_reference: NonEmptyStr | None = None
    human_required_reason: HumanRequiredReason | None = None
    detail: NonEmptyStr | None = None


@runtime_checkable
class BrowserApplicationDriver(Protocol):
    """Prepares and submits one application on one ATS (§3).

    `supports` nominates the driver from the target URL; `prepare` reads the form (reversible,
    never clicks the final control, §19); `submit` fills the cleared answers, uploads the pinned
    files and clicks the one irreversible control at most once (§20). A driver reports outcomes as
    the typed values above and never raises an ATS-specific exception across this boundary.
    """

    @property
    def driver_key(self) -> str:
        """A stable provenance stamp for the audit trail, e.g. ``greenhouse/1``."""
        ...

    @property
    def platform(self) -> str:
        """The platform label, e.g. ``greenhouse``."""
        ...

    def supports(self, target_url: str | None) -> bool:
        """Whether this driver's URL patterns nominate it for a target (§4)."""
        ...

    async def prepare(self, page: Page, context: DriverContext) -> PreparedBrowserForm:
        """Navigate, detect challenges, inspect the form, and fingerprint it — never submit."""
        ...

    async def submit(self, page: Page, context: DriverContext) -> BrowserSubmissionEvidence:
        """Fill the cleared answers, upload the pinned files, and click submit at most once."""
        ...
