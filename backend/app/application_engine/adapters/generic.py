"""The generic adapter: the honest floor for any channel with no automation (§12-13).

Every channel the engine cannot drive specifically resolves to this adapter (through
`ApplicationAdapterRegistry.resolve`), and its whole job is to *fail conservatively*.
It declares `MANUAL_ONLY`, so the execution gate can never authorize it to submit
unattended however permissive the policy; it prepares nothing it cannot verify; and
it always reports that a human must take it from here. That is the design §13 asks
for: an unknown channel becomes a clear "you need to apply yourself" hand-off, never
a silent auto-submission and never an exception a user cannot act on.

It is `MANUAL_ONLY` rather than `UNSUPPORTED` on purpose — the platform *has* prepared
the candidate's documents by the time an application reaches an adapter, so there is
something to hand the human. `UNSUPPORTED` would claim there is nothing at all.
"""
from backend.app.application_engine.contracts import (
    AdapterCapabilities,
    AdapterPreparation,
    ApplicationContext,
)
from backend.app.domain.application import SubmissionOutcome, SubmissionResult
from backend.app.domain.application_channel import (
    AdapterSafetyLevel,
    ApplicationChannel,
    HumanRequiredReason,
)

GENERIC_ADAPTER_KEY = "generic-manual/1"


class GenericManualAdapter:
    """Prepares what it can, submits nothing, always asks for a human (§13).

    The registry's fallback. `prepare` succeeds — there is no form it pretends to
    read, so it reports no questions and no fingerprint — but it flags
    `UNSUPPORTED_CHANNEL`, which the service turns into a REQUIRES_HUMAN application.
    `submit` must never be reached (the gate stops a `MANUAL_ONLY` adapter before
    submission), but if it somehow is, it refuses with a REQUIRES_HUMAN result rather
    than doing anything irreversible.
    """

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            key=GENERIC_ADAPTER_KEY,
            channel=ApplicationChannel.MANUAL,
            safety_level=AdapterSafetyLevel.MANUAL_ONLY,
            can_prepare=True,
            can_submit=False,
        )

    async def prepare(self, context: ApplicationContext) -> AdapterPreparation:
        return AdapterPreparation(
            human_required_reasons=(HumanRequiredReason.UNSUPPORTED_CHANNEL,),
            detail="this channel has no automated path; a human must apply",
        )

    async def submit(self, context: ApplicationContext) -> SubmissionResult:
        # Defence in depth: the gate never authorizes a MANUAL_ONLY submission, so
        # reaching here is a bug upstream — and the safe response to that bug is to
        # refuse and hand off, never to attempt an irreversible action.
        return SubmissionResult(
            outcome=SubmissionOutcome.REQUIRES_HUMAN,
            human_required_reason=HumanRequiredReason.UNSUPPORTED_CHANNEL,
            detail="the generic adapter cannot submit; a human must apply",
        )
