"""The driver-agnostic orchestrator: the safety that must not live in any single driver (§14-23).

A driver knows how one ATS works; it must not be trusted with the invariants that keep an
autonomous submission safe, because a bug in one driver would then be a safety bug. So those live
here, once, around every driver:

- **PREPARE** reads the form through the driver and hands its questions and profile-backed proposals
  up for the service to resolve (§9, §12) — it decides nothing about answers itself.
- **SUBMIT** re-inspects the form first (reversible), and *before* any irreversible act it: refuses
  a form whose fingerprint changed since preparation (§14); refuses a required field with no cleared
  answer (§11) or a required sensitive field (§10); refuses a required upload with no pinned file
  (§13). Only then does it let the driver perform the one irreversible click (§20), and it maps the
  driver's evidence to an honest outcome — a confirmed send is COMPLETED, an unconfirmed one is
  STATE_UNKNOWN, never a retryable failure (§21-22).

Every outcome carries the driver's provenance (`driver_key`, `platform`) for the audit trail (§24),
and every `detail` is a composed, secret-free sentence (§33).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.app.application_engine.browser.contracts import (
    BrowserApplicationDriver,
    BrowserSubmissionEvidence,
    DriverContext,
    PreparedBrowserForm,
)
from backend.app.application_engine.browser.profile_answers import propose_from_profile
from backend.app.application_engine.browser.semantics import COVER_LETTER_SLOT, RESUME_SLOT
from backend.app.application_engine.task_dispatcher import (
    BrowserTaskOutcome,
    BrowserTaskResult,
)
from backend.app.domain.application_answer import (
    ApplicationQuestion,
    ApplicationQuestionKind,
)
from backend.app.domain.application_channel import HumanRequiredReason
from backend.app.domain.candidate import CandidateProfile
from backend.app.domain.documents import CandidateDocumentType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.async_api import Page

_UPLOAD_SLOT_TYPES: dict[str, CandidateDocumentType] = {
    RESUME_SLOT: CandidateDocumentType.RESUME,
    COVER_LETTER_SLOT: CandidateDocumentType.COVER_LETTER,
}


async def run_prepare(
        page: Page, driver: BrowserApplicationDriver, context: DriverContext,
        profile: CandidateProfile) -> BrowserTaskResult:
    """Read the form and report its questions and profile-backed proposals (§9, §12)."""
    form = await driver.prepare(page, context)
    if form.blocked:
        return _human(form, driver)
    proposals = propose_from_profile(form.questions, profile)
    return BrowserTaskResult(
        outcome=BrowserTaskOutcome.COMPLETED,
        questions=form.questions,
        answer_proposals=proposals,
        form_fingerprint=form.form_fingerprint,
        driver_key=driver.driver_key,
        platform=form.platform,
        detail=form.detail)


async def run_submit(
        page: Page, driver: BrowserApplicationDriver,
        context: DriverContext) -> BrowserTaskResult:
    """Re-check every safety invariant, then let the driver perform the one click (§14-22)."""
    form = await driver.prepare(page, context)
    if form.blocked:
        return _human(form, driver)

    if context.expected_form_fingerprint is None:
        return _requires_human(
            driver, form.platform, HumanRequiredReason.AMBIGUOUS_FORM,
            "the application form has not been safely prepared")

    if form.form_fingerprint != context.expected_form_fingerprint:
        # §14: the form moved under the prepared answers. Do not fill, do not click.
        return _requires_human(
            driver, form.platform, HumanRequiredReason.FORM_CHANGED,
            "the form changed after preparation and must be reviewed again")

    blocking = _first_blocking_field(form.questions, context)
    if blocking is not None:
        reason, detail = blocking
        return _requires_human(driver, form.platform, reason, detail)

    evidence = await driver.submit(page, context)
    return _evidence_to_result(evidence, driver, form.platform)


def _first_blocking_field(
        questions: tuple[ApplicationQuestion, ...],
        context: DriverContext) -> tuple[HumanRequiredReason, str] | None:
    """The first required field the submission cannot safely satisfy, or `None` (§10-13).

    A defence-in-depth re-check of what the service already resolved at preparation: a required
    sensitive field is never auto-answered (§10); a required field with no cleared answer is a
    human hand-off, never a guess (§11); a required upload with no pinned file is unresolved (§13).
    """
    for question in questions:
        if not question.required:
            continue
        if question.sensitivity.needs_human:
            return (HumanRequiredReason.SENSITIVE_QUESTION,
                    "a required sensitive question must be answered by a human")
        if question.kind is ApplicationQuestionKind.FILE_UPLOAD:
            document_type = _UPLOAD_SLOT_TYPES.get(question.key)
            if document_type is None or context.upload_for(document_type) is None:
                return (HumanRequiredReason.UPLOAD_UNRESOLVED,
                        "a required document upload has no pinned file")
            continue
        if context.answer_for(question.key) is None:
            return (HumanRequiredReason.UNKNOWN_REQUIRED_FIELD,
                    "a required field has no trustworthy answer and needs a human")
    return None


def _evidence_to_result(
        evidence: BrowserSubmissionEvidence, driver: BrowserApplicationDriver,
        platform: str) -> BrowserTaskResult:
    """Map a driver's submission evidence to the outcome vocabulary the adapter reads (§20-22)."""
    if evidence.human_required_reason is not None:
        return _requires_human(
            driver, platform, evidence.human_required_reason,
            evidence.detail or "a human is required to complete the submission")
    if evidence.clicked and evidence.confirmed:
        return BrowserTaskResult(
            outcome=BrowserTaskOutcome.COMPLETED,
            submission_confirmed=True,
            confirmation_reference=evidence.confirmation_reference,
            driver_key=driver.driver_key, platform=platform,
            detail=evidence.detail or "the submission was confirmed")
    if evidence.clicked:
        # Clicked but unconfirmed: it may have reached the employer, so never retry (§22).
        return BrowserTaskResult(
            outcome=BrowserTaskOutcome.STATE_UNKNOWN,
            driver_key=driver.driver_key, platform=platform,
            detail=evidence.detail or "submitted but the outcome could not be confirmed")
    # Never clicked and no human stop: a pre-click failure, safe to retry (§23).
    return BrowserTaskResult(
        outcome=BrowserTaskOutcome.FAILED,
        driver_key=driver.driver_key, platform=platform,
        detail=evidence.detail or "the submission failed before anything was sent")


def _human(form: PreparedBrowserForm, driver: BrowserApplicationDriver) -> BrowserTaskResult:
    """A REQUIRES_HUMAN result from a blocked preparation, carrying the driver's provenance."""
    assert form.human_required_reason is not None  # `blocked` guarantees it
    return _requires_human(
        driver, form.platform, form.human_required_reason,
        form.detail or "a human is required to continue")


def _requires_human(
        driver: BrowserApplicationDriver, platform: str, reason: HumanRequiredReason,
        detail: str) -> BrowserTaskResult:
    return BrowserTaskResult(
        outcome=BrowserTaskOutcome.REQUIRES_HUMAN,
        human_required_reason=reason,
        driver_key=driver.driver_key, platform=platform, detail=detail)
