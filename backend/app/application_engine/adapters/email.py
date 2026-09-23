"""The email adapter: applies by sending to a stated address (§62).

An application by email is an irreversible send, so this adapter is built around the
one rule §62 insists on: *the recipient must be explicit*. The engine never guesses a
hiring address off a page or a domain, because a wrong guess emails a stranger a
candidate's CV. So the recipient arrives on the context (`recipient_email`), set by a
human or a verified source; if it is absent, the adapter hands off to a human rather
than inventing one.

The message body is composed by the platform, never lifted from an untrusted page
(§66): the adapter assembles a short, fixed cover note plus the candidate's pinned,
guard-cleared documents as attachments (§14-16). A document that is not a rendered,
cleared artifact is refused with `APPLICATION_DOCUMENT_NOT_READY` — there is nothing
safe to attach — rather than sent as a broken application.

Its ceiling is `SUPPORTED_WITH_REVIEW`: an email cannot be unsent, so a human approves
the send whatever the policy says, exactly as with the browser channel.
"""
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from backend.app.application_engine.contracts import (
    AdapterCapabilities,
    AdapterPreparation,
    ApplicationContext,
)
from backend.app.domain.application import (
    PinnedDocument,
    SubmissionOutcome,
    SubmissionResult,
)
from backend.app.domain.application_channel import (
    AdapterSafetyLevel,
    ApplicationChannel,
    HumanRequiredReason,
)
from backend.app.domain.application_failure import ApplicationFailureCode
from backend.app.domain.base import NonEmptyStr
from backend.app.domain.documents import CandidateDocument, DocumentStatus

EMAIL_ADAPTER_KEY = "email/1"


class EmailAttachment(BaseModel):
    """One file to attach, as a storage-neutral locator plus its display name.

    `storage_key` is the `DocumentArtifactRef.storage_key` of a rendered version —
    opaque to this layer, resolved by whatever store the sender uses. No bytes travel
    through the domain or the adapter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    filename: NonEmptyStr
    storage_key: NonEmptyStr
    media_type: NonEmptyStr = "application/pdf"


class EmailMessage(BaseModel):
    """A composed application email, ready for a sender to deliver.

    Every field is composed by the platform: the recipient is the explicit address,
    the subject and body are a fixed, neutral template (no page text), and the
    attachments are the candidate's pinned rendered documents.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recipient: NonEmptyStr
    subject: NonEmptyStr
    body: NonEmptyStr
    attachments: tuple[EmailAttachment, ...] = ()


class EmailSendError(Exception):
    """A hard delivery failure: the message was rejected before it was accepted.

    Distinct from an ambiguous send: this means the send did not begin (a refused
    connection, an invalid address), so the application was not submitted and a retry
    is safe. The adapter maps it to a plain `FAILED`, never `STATE_UNKNOWN`.
    """


@runtime_checkable
class EmailSender(Protocol):
    """Delivers a composed message and returns the provider's reference (§62).

    `async`, like every I/O port here. On success it returns a non-secret reference
    (a message id) for the audit trail; on a hard failure it raises `EmailSendError`.
    It never returns a page's or server's raw reply — a detail is composed, not
    forwarded (§83).
    """

    async def send(self, message: EmailMessage) -> str:
        ...


class FakeEmailSender:
    """An in-process sender that records messages and returns a stub reference.

    The test double: it never opens a connection, and `sent` lets a test assert an
    application was emailed exactly once (the §37 idempotency check counts sends).
    Constructed with `fail=True`, it raises `EmailSendError` to exercise the failure
    path.
    """

    def __init__(self, *, fail: bool = False, reference: str = "fake-message-1") -> None:
        self._fail = fail
        self._reference = reference
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> str:
        if self._fail:
            raise EmailSendError("the fake sender was configured to fail")
        self.sent.append(message)
        return self._reference


class EmailApplicationAdapter:
    """Applies by emailing pinned documents to an explicit recipient (§62).

    Prepares only when it has a recipient and something to attach; submits by handing
    a fully-composed `EmailMessage` to an `EmailSender`. It resolves each pinned
    document to its rendered artifact and refuses if any pinned version is not a
    cleared, rendered one — an email application with a missing or unvalidated
    attachment is not sent.
    """

    def __init__(self, sender: EmailSender) -> None:
        self._sender = sender

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            key=EMAIL_ADAPTER_KEY,
            channel=ApplicationChannel.EMAIL,
            safety_level=AdapterSafetyLevel.SUPPORTED_WITH_REVIEW,
            can_prepare=True,
            can_submit=True,
        )

    async def prepare(self, context: ApplicationContext) -> AdapterPreparation:
        if context.recipient_email is None:
            return AdapterPreparation(
                human_required_reasons=(HumanRequiredReason.UNKNOWN_REQUIRED_FIELD,),
                detail="no recipient address is known; the platform will not guess one")
        # The fingerprint is the recipient plus the pinned version ids: if either the
        # address or the exact documents change after preparation, the submit-time
        # re-check catches it, exactly as a form fingerprint would.
        fingerprint = _email_fingerprint(context.recipient_email,
                                        context.pinned_documents)
        return AdapterPreparation(form_fingerprint=fingerprint)

    async def submit(self, context: ApplicationContext) -> SubmissionResult:
        recipient = context.recipient_email
        if recipient is None:
            return SubmissionResult(
                outcome=SubmissionOutcome.REQUIRES_HUMAN,
                human_required_reason=HumanRequiredReason.UNKNOWN_REQUIRED_FIELD,
                detail="no recipient address is known; the platform will not guess one")

        # §34: the "form" for an email is its recipient and attachments; if either
        # changed since preparation, the prepared application no longer matches, so
        # stop for a human rather than send something different from what was reviewed.
        current = _email_fingerprint(recipient, context.pinned_documents)
        if context.expected_form_fingerprint is not None \
                and current != context.expected_form_fingerprint:
            return SubmissionResult(
                outcome=SubmissionOutcome.REQUIRES_HUMAN,
                human_required_reason=HumanRequiredReason.FORM_CHANGED,
                detail="the recipient or attachments changed after preparation")

        try:
            attachments = _resolve_attachments(context.pinned_documents,
                                              context.documents)
        except _DocumentNotReady as exc:
            return SubmissionResult(
                outcome=SubmissionOutcome.FAILED,
                failure_code=ApplicationFailureCode.APPLICATION_DOCUMENT_NOT_READY,
                detail=exc.detail)

        message = EmailMessage(
            recipient=recipient,
            subject=_subject(context),
            body=_body(context),
            attachments=attachments,
        )
        try:
            reference = await self._sender.send(message)
        except EmailSendError as exc:
            return SubmissionResult(
                outcome=SubmissionOutcome.FAILED,
                failure_code=ApplicationFailureCode.APPLICATION_ADAPTER_ERROR,
                detail=str(exc)[:200] or "the email could not be sent")
        return SubmissionResult(
            outcome=SubmissionOutcome.SUBMITTED, confirmation_reference=reference)


class _DocumentNotReady(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _resolve_attachments(
    pinned: tuple[PinnedDocument, ...],
    documents: tuple[CandidateDocument, ...],
) -> tuple[EmailAttachment, ...]:
    """Turn each pinned version into an attachment, or refuse if any is not ready.

    A pinned version must be exactly the RENDERED, guard-cleared artifact recorded on
    the document (§15). Anything else — a pin at a version that was never rendered, a
    document the context did not carry — means there is nothing safe to attach.
    """
    by_id = {document.id: document for document in documents}
    attachments: list[EmailAttachment] = []
    for pin in pinned:
        document = by_id.get(pin.document_id)
        if document is None:
            raise _DocumentNotReady(
                "a pinned document is not available for this application")
        version = document.version(pin.version)
        if version is None or version.status is not DocumentStatus.RENDERED \
                or version.artifact is None:
            raise _DocumentNotReady(
                "a pinned document version is not a rendered, cleared artifact")
        attachments.append(EmailAttachment(
            filename=f"{pin.document_type.value.lower()}.pdf",
            storage_key=version.artifact.storage_key,
            media_type=version.artifact.media_type))
    return tuple(attachments)


def _email_fingerprint(recipient: str,
                       pinned: tuple[PinnedDocument, ...]) -> str:
    versions = ",".join(sorted(str(pin.version_id) for pin in pinned))
    return f"email:{recipient}:{versions}"


def _subject(context: ApplicationContext) -> str:
    if context.opportunity is not None:
        return f"Application: {context.opportunity.title}"
    if context.company is not None:
        return f"Spontaneous application: {context.company.name}"
    return "Application"


def _body(context: ApplicationContext) -> str:
    """A neutral, platform-composed note; never text lifted from a page (§66)."""
    return ("Please find my application attached. I would welcome the opportunity to "
            "discuss how I can contribute.")
