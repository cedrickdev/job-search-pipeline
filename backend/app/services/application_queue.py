"""Owner-scoped ingress to the durable browser submission lane."""
from backend.app.application_engine.browser.form_capabilities import FormCapabilityCatalog
from backend.app.domain.application import ApplicationState
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.identifiers import ApplicationId, TaskRunId, UserId
from backend.app.domain.policy import AutomationMode
from backend.app.domain.task import TaskKind, TaskSpec
from backend.app.repositories.contracts import (
    ApplicationPolicyRepository,
    ApplicationRepository,
    OpportunityRepository,
)
from backend.app.services.applications import ApplicationNotActionable, ApplicationNotFound
from backend.app.tasks.dispatcher import TaskDispatcher


class BrowserSubmissionDisabled(Exception):
    """The deployment has not enabled live browser submissions."""


class BrowserSubmissionNotPermitted(Exception):
    """The current policy, channel, or country does not permit browser queueing."""


class ApplicationQueueService:
    def __init__(self, applications: ApplicationRepository,
                 policies: ApplicationPolicyRepository,
                 opportunities: OpportunityRepository,
                 dispatcher: TaskDispatcher, *, live_enabled: bool,
                 form_catalog: FormCapabilityCatalog | None = None) -> None:
        self._applications = applications
        self._policies = policies
        self._opportunities = opportunities
        self._dispatcher = dispatcher
        self._live_enabled = live_enabled
        self._form_catalog = form_catalog or FormCapabilityCatalog(forms=())

    async def enqueue_approved(self, user_id: UserId,
                               app_id: ApplicationId) -> TaskRunId:
        app = await self._applications.get(user_id, app_id)
        if app is None:
            raise ApplicationNotFound(str(app_id))
        if app.state is not ApplicationState.APPROVED:
            raise ApplicationNotActionable(app.state, "queue")
        if not self._live_enabled:
            raise BrowserSubmissionDisabled()
        policy = await self._policies.get_default(user_id)
        if (policy is None or not policy.is_active
                or policy.mode not in (AutomationMode.SUPERVISED,
                                       AutomationMode.AUTOPILOT)
                or app.channel is not ApplicationChannel.BROWSER
                or app.opportunity_id is None):
            raise BrowserSubmissionNotPermitted()
        opportunity = await self._opportunities.get(app.opportunity_id)
        if (opportunity is None or opportunity.location is None
                or opportunity.location.country != "CH"
                or opportunity.application_url is None):
            raise BrowserSubmissionNotPermitted()
        if app.form_capability_id is None:
            raise BrowserSubmissionNotPermitted()
        decision = self._form_catalog.resolve(
            str(opportunity.application_url), app.form_driver_key,
            app.form_fingerprint)
        if decision.capability_id != app.form_capability_id:
            raise BrowserSubmissionNotPermitted()
        return await self._dispatcher.enqueue(TaskSpec(
            kind=TaskKind.APPLICATION_SUBMISSION,
            idempotency_key=f"application-submission:{app.id}",
            user_id=user_id, payload={"application_id": str(app.id)},
            max_attempts=1))
