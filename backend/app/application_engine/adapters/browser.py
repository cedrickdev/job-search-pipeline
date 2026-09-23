"""The browser adapter: drives a page for the `BROWSER` channel, at arm's length.

Playwright never appears here (CLAUDE.md). This adapter turns a `prepare`/`submit`
call into a typed `BrowserTask`, hands it to a `TaskDispatcher`, and translates the
typed `BrowserTaskResult` back — so the adapter is pure translation and the browser
runs in whatever isolated place the dispatcher chooses. A test injects a
`FakeTaskDispatcher` and no browser starts.

Its safety ceiling is `SUPPORTED_WITH_REVIEW`, never `FULLY_SUPPORTED` (§59). A page
the platform drives can present a login wall, a CAPTCHA or a changed form at any
moment, and the honest ceiling for "we automated a browser" is that a human approves
the actual submission. The gate takes the minimum of this and the policy, so even an
AUTOPILOT policy still stops for review on a browser channel — the adapter lowers
autonomy, and cannot raise it.
"""
from backend.app.application_engine.contracts import (
    AdapterCapabilities,
    AdapterPreparation,
    ApplicationContext,
)
from backend.app.application_engine.task_dispatcher import (
    BrowserBusy,
    BrowserTask,
    BrowserTaskKind,
    BrowserTaskOutcome,
    BrowserTaskResult,
    TaskDispatcher,
)
from backend.app.domain.application import SubmissionOutcome, SubmissionResult
from backend.app.domain.application_channel import (
    AdapterSafetyLevel,
    ApplicationChannel,
    HumanRequiredReason,
)
from backend.app.domain.application_failure import ApplicationFailureCode

BROWSER_ADAPTER_KEY = "browser/1"


class BrowserApplicationAdapter:
    """Prepares and submits through a browser, entirely via a `TaskDispatcher`.

    Holds no browser and no lock of its own — the dispatcher (and, under it, the
    worker) own those. The adapter's only logic is mapping the two typed vocabularies
    onto each other, and doing so *safely*: an ambiguous submit becomes
    `STATE_UNKNOWN` (never a retryable failure), and a busy browser becomes a plain
    failure at submit time because nothing was sent, so a later attempt is safe.
    """

    def __init__(self, dispatcher: TaskDispatcher) -> None:
        self._dispatcher = dispatcher

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            key=BROWSER_ADAPTER_KEY,
            channel=ApplicationChannel.BROWSER,
            safety_level=AdapterSafetyLevel.SUPPORTED_WITH_REVIEW,
            can_prepare=True,
            can_submit=True,
        )

    def _target_url(self, context: ApplicationContext) -> str | None:
        return (context.opportunity.application_url
                if context.opportunity is not None else None)

    async def prepare(self, context: ApplicationContext) -> AdapterPreparation:
        """Read the form through a PREPARE task and report what it asks.

        A PREPARE task is reversible, so a busy browser or a failure is simply "could
        not prepare yet" — reported as a human hand-off rather than raised, so the
        service can surface it and the run can be retried later without any risk.
        """
        task = BrowserTask(
            application_id=context.application.id,
            kind=BrowserTaskKind.PREPARE,
            target_url=self._target_url(context),
            correlation_id=context.correlation_id,
        )
        try:
            result = await self._dispatcher.run(task)
        except BrowserBusy:
            return AdapterPreparation(
                human_required_reasons=(HumanRequiredReason.LOGIN_REQUIRED,),
                detail="the browser is in use; preparation could not run yet")

        if result.outcome is BrowserTaskOutcome.COMPLETED:
            return AdapterPreparation(
                form_fingerprint=result.form_fingerprint, detail=result.detail)
        if result.outcome is BrowserTaskOutcome.REQUIRES_HUMAN \
                and result.human_required_reason is not None:
            return AdapterPreparation(
                human_required_reasons=(result.human_required_reason,),
                detail=result.detail)
        # FAILED / STATE_UNKNOWN during a reversible prepare: nothing was submitted,
        # so it is safe to report a hand-off and let the service retry preparation.
        return AdapterPreparation(
            human_required_reasons=(HumanRequiredReason.AMBIGUOUS_FORM,),
            detail=result.detail or "preparation did not complete")

    async def submit(self, context: ApplicationContext) -> SubmissionResult:
        """Submit through a SUBMIT task; the one irreversible act, mapped safely."""
        task = BrowserTask(
            application_id=context.application.id,
            kind=BrowserTaskKind.SUBMIT,
            target_url=self._target_url(context),
            correlation_id=context.correlation_id,
        )
        try:
            result = await self._dispatcher.run(task)
        except BrowserBusy:
            # The lock was never acquired, so nothing was submitted: a plain failure
            # is safe to retry. This is the one place a busy browser is *not*
            # STATE_UNKNOWN, precisely because no irreversible act began.
            return SubmissionResult(
                outcome=SubmissionOutcome.FAILED,
                failure_code=ApplicationFailureCode.APPLICATION_ADAPTER_ERROR,
                detail="the browser was in use; the submission did not start")
        return _to_submission_result(result)


def _to_submission_result(result: BrowserTaskResult) -> SubmissionResult:
    """Translate a browser task result into a submission result, outcome for outcome."""
    if result.outcome is BrowserTaskOutcome.COMPLETED:
        return SubmissionResult(
            outcome=SubmissionOutcome.SUBMITTED, detail=result.detail)
    if result.outcome is BrowserTaskOutcome.REQUIRES_HUMAN:
        return SubmissionResult(
            outcome=SubmissionOutcome.REQUIRES_HUMAN,
            human_required_reason=result.human_required_reason,
            detail=result.detail)
    if result.outcome is BrowserTaskOutcome.STATE_UNKNOWN:
        return SubmissionResult(
            outcome=SubmissionOutcome.STATE_UNKNOWN, detail=result.detail)
    return SubmissionResult(
        outcome=SubmissionOutcome.FAILED,
        failure_code=ApplicationFailureCode.APPLICATION_ADAPTER_ERROR,
        detail=result.detail)
