# tests/test_v2_chat_executor.py
"""Executing a confirmed proposal: the last gate, and the only place an action runs.

These tests pin the four properties that make the executor safe to hand a confirmed
proposal. It re-authorizes before it acts (a validator refusal is recorded, never run) and
the service it calls is re-entered for its own authoritative checks. It is idempotent by
the proposal's derived id (a recorded execution short-circuits, so the underlying action
never runs twice). Every attempt is audited — SUCCEEDED, REJECTED or FAILED — and the
proposal's status is moved to match. And a detail never leaks a secret: a typed precondition
refusal surfaces the service's own secret-free message, but an *unexpected* exception is
recorded with a fixed generic sentence and its raw text is dropped. Read-only actions are
recorded SUCCEEDED without any service call at all.
"""
import pytest

from backend.app.chat.executor import (
    _UNEXPECTED_FAILURE_DETAIL,
    ChatActionExecutor,
    ChatProposalNotActionable,
    ChatProposalNotFound,
)
from backend.app.chat.validators import (
    ProposalRejectionCode,
    ProposalValidation,
    ProposalValidator,
)
from backend.app.domain.application import (
    Application,
    ApplicationState,
    build_idempotency_key,
)
from backend.app.domain.application_channel import ApplicationChannel
from backend.app.domain.application_failure import (
    ApplicationError,
    ApplicationFailureCode,
)
from backend.app.domain.chat import (
    ChatActionExecutionOutcome,
    ChatActionProposalStatus,
    ConversationScope,
    CreateApplicationAction,
    GenerateResumeAction,
    NavigateAction,
    NavigationTarget,
    OpenInterviewPrepAction,
    SetSearchRadiusAction,
    SubmitApplicationAction,
    UpdateSearchKeywordsAction,
)
from backend.app.domain.identifiers import (
    application_id,
    chat_action_execution_id,
    new_application_decision_id,
)
from backend.app.documents import InsufficientEvidence
from backend.app.services.applications import ApplicationNotActionable
from tests.v2_builders import (
    APPLICATION,
    CONVERSATION,
    NOW,
    OPPORTUNITY,
    OTHER_APPLICATION,
    OTHER_OPPORTUNITY,
    OTHER_USER,
    PROFILE,
    SEARCH_PROFILE,
    USER,
    a_chat_action_execution,
    a_chat_action_proposal,
    a_conversation,
    a_rendered_document,
    a_search_profile,
)
from tests.v2_fakes import (
    FakeApplicationRepository,
    FakeChatActionExecutionRepository,
    FakeChatActionProposalRepository,
    FakeConversationRepository,
    FakeOpportunityRepository,
    FakeSearchProfileRepository,
)

# A tripwire: a service stub the executor must NOT call raises this, so a mistaken dispatch
# fails loudly rather than passing silently.
_MUST_NOT_RUN = RuntimeError("this service must not be called on this path")


def _an_application(*, state: ApplicationState) -> Application:
    key = build_idempotency_key(
        candidate_profile_id=PROFILE, channel=ApplicationChannel.BROWSER,
        opportunity_id=OPPORTUNITY, company_id=None)
    return Application(
        id=application_id(key), user_id=USER, candidate_profile_id=PROFILE,
        decision_id=new_application_decision_id(), channel=ApplicationChannel.BROWSER,
        state=state, idempotency_key=key, opportunity_id=OPPORTUNITY,
        company_id=None, created_at=NOW, updated_at=NOW)


class _StubValidator:
    """Stands in for `ProposalValidator`: returns a fixed verdict and records the call.

    A stub rather than the real validator, so an executor test controls the authorization
    outcome directly and stays about executor behaviour; one integration test below uses
    the real validator to prove the executor actually consults it.
    """

    def __init__(self, verdict: ProposalValidation | None = None) -> None:
        self.calls: list[object] = []
        self._verdict = verdict if verdict is not None else ProposalValidation.permit()

    async def validate(self, user_id, action, *, conversation=None) -> ProposalValidation:
        self.calls.append((user_id, action, conversation))
        return self._verdict


class _StubApplications:
    """Records which lifecycle method was called; returns a canned app or raises."""

    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, object, object]] = []
        self._result = result
        self._error = error

    async def _run(self, op: str, user_id, target):
        self.calls.append((op, user_id, target))
        if self._error is not None:
            raise self._error
        return self._result

    async def create(self, user_id, opportunity_id, *, now):
        return await self._run("create", user_id, opportunity_id)

    async def prepare(self, user_id, application_id, *, now):
        return await self._run("prepare", user_id, application_id)

    async def approve(self, user_id, application_id, *, now):
        return await self._run("approve", user_id, application_id)

    async def submit(self, user_id, application_id, *, now):
        return await self._run("submit", user_id, application_id)

    async def cancel(self, user_id, application_id, *, now):
        return await self._run("cancel", user_id, application_id)


class _StubDocuments:
    """Records a generate call; returns a canned document or raises."""

    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.calls: list[tuple[object, object, object, object]] = []
        self._result = result
        self._error = error

    async def generate(self, user_id, opportunity_id, document_type, *, now,
                        language=None):
        self.calls.append((user_id, opportunity_id, document_type, language))
        if self._error is not None:
            raise self._error
        return self._result


class _StubOnboarding:
    """Records a search edit; returns a canned search or raises."""

    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._result = result
        self._error = error

    async def set_search_radius(self, user_id, search_profile_id, *, radius_km, now):
        self.calls.append(("set_search_radius", search_profile_id, radius_km))
        if self._error is not None:
            raise self._error
        return self._result

    async def set_search_keywords(self, user_id, search_profile_id, *,
                                  title_keywords=None, excluded_keywords=None, now):
        self.calls.append(
            ("set_search_keywords", search_profile_id, title_keywords, excluded_keywords))
        if self._error is not None:
            raise self._error
        return self._result


def _conversations_with_global() -> FakeConversationRepository:
    """A store holding the default GLOBAL thread the seeded proposals belong to.

    The executor loads a proposal's conversation to hand the validator its scope; the
    proposals here are built for `CONVERSATION`, which is GLOBAL, so the scope wall is a
    no-op and these tests stay about re-authorization, dispatch and audit — the scope wall
    itself is exercised in the dedicated scope tests.
    """
    repo = FakeConversationRepository()
    repo.conversations[CONVERSATION] = a_conversation()
    return repo


def _executor(*, proposals, executions, validator=None, documents=None,
              applications=None, onboarding=None, conversations=None
              ) -> ChatActionExecutor:
    return ChatActionExecutor(
        proposals=proposals, executions=executions,
        conversations=(conversations if conversations is not None
                       else _conversations_with_global()),
        validator=validator if validator is not None else _StubValidator(),
        documents=documents if documents is not None else _StubDocuments(),
        applications=applications if applications is not None else _StubApplications(),
        onboarding=onboarding if onboarding is not None else _StubOnboarding())


async def _seed(proposals: FakeChatActionProposalRepository, proposal) -> None:
    await proposals.upsert(proposal)


# --- the guards before anything runs ---------------------------------------

@pytest.mark.asyncio
async def test_a_missing_proposal_raises_not_found():
    proposals = FakeChatActionProposalRepository()
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository())
    proposal = a_chat_action_proposal()
    with pytest.raises(ChatProposalNotFound):
        await executor.execute(USER, proposal.id, now=NOW)


@pytest.mark.asyncio
async def test_a_proposal_owned_by_another_account_reads_as_not_found():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(user_id=OTHER_USER)
    await _seed(proposals, proposal)
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository())
    with pytest.raises(ChatProposalNotFound):
        await executor.execute(USER, proposal.id, now=NOW)


@pytest.mark.asyncio
async def test_a_recorded_execution_is_returned_without_re_running():
    proposals = FakeChatActionProposalRepository()
    executions = FakeChatActionExecutionRepository()
    proposal = a_chat_action_proposal()
    await _seed(proposals, proposal)
    await executions.upsert(a_chat_action_execution(proposal_id=proposal.id))
    # A service that would explode if the executor mistakenly re-ran the action.
    applications = _StubApplications(error=_MUST_NOT_RUN)
    executor = _executor(proposals=proposals, executions=executions,
                         applications=applications)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.SUCCEEDED
    assert result.detail == "Candidature soumise"  # the seeded audit, unchanged
    assert applications.calls == []
    # The proposal was not touched by this short-circuit; it stays PROPOSED.
    reloaded = await proposals.get(USER, proposal.id)
    assert reloaded is not None and reloaded.is_open


@pytest.mark.asyncio
async def test_a_dismissed_proposal_raises_not_actionable():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(status=ChatActionProposalStatus.DISMISSED)
    await _seed(proposals, proposal)
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository())
    with pytest.raises(ChatProposalNotActionable) as caught:
        await executor.execute(USER, proposal.id, now=NOW)
    assert caught.value.status is ChatActionProposalStatus.DISMISSED


# --- re-authorization: a refusal is recorded, never run --------------------

@pytest.mark.asyncio
async def test_a_validator_refusal_is_recorded_as_rejected_and_no_service_runs():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal()  # a SubmitApplicationAction
    await _seed(proposals, proposal)
    validator = _StubValidator(ProposalValidation.reject(
        ProposalRejectionCode.APPLICATION_CLOSED, "application is CANCELLED"))
    applications = _StubApplications(error=_MUST_NOT_RUN)
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         validator=validator, applications=applications)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.REJECTED
    assert result.detail == "application is CANCELLED"
    assert result.result_ref == ProposalRejectionCode.APPLICATION_CLOSED.value
    assert result.id == chat_action_execution_id(proposal.id)
    assert applications.calls == []
    reloaded = await proposals.get(USER, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ChatActionProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_a_read_only_navigate_succeeds_without_a_service_call():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=NavigateAction(target=NavigationTarget.OPPORTUNITIES))
    await _seed(proposals, proposal)
    executor = _executor(
        proposals=proposals, executions=FakeChatActionExecutionRepository(),
        applications=_StubApplications(error=_MUST_NOT_RUN),
        documents=_StubDocuments(error=_MUST_NOT_RUN),
        onboarding=_StubOnboarding(error=_MUST_NOT_RUN))

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.SUCCEEDED
    assert result.result_ref == NavigationTarget.OPPORTUNITIES.value
    assert "navigate to OPPORTUNITIES" in (result.detail or "")
    reloaded = await proposals.get(USER, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ChatActionProposalStatus.EXECUTED


@pytest.mark.asyncio
async def test_open_interview_prep_records_the_opportunity_as_the_hint():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=OpenInterviewPrepAction(opportunity_id=OPPORTUNITY))
    await _seed(proposals, proposal)
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository())

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.SUCCEEDED
    assert result.result_ref == str(OPPORTUNITY)
    assert "open interview prep" in (result.detail or "")


# --- mutating actions reach exactly one service and record what it produced -

@pytest.mark.asyncio
async def test_a_submit_success_records_the_new_state_and_marks_executed():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=SubmitApplicationAction(application_id=APPLICATION))
    await _seed(proposals, proposal)
    submitted = _an_application(state=ApplicationState.SUBMITTED)
    applications = _StubApplications(result=submitted)
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         applications=applications)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.SUCCEEDED
    assert result.result_ref == str(submitted.id)
    assert result.detail == "application is now SUBMITTED"
    assert applications.calls == [("submit", USER, APPLICATION)]
    reloaded = await proposals.get(USER, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ChatActionProposalStatus.EXECUTED


@pytest.mark.asyncio
async def test_a_create_success_records_the_opened_application():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=CreateApplicationAction(opportunity_id=OPPORTUNITY))
    await _seed(proposals, proposal)
    planned = _an_application(state=ApplicationState.PLANNED)
    applications = _StubApplications(result=planned)
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         applications=applications)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.SUCCEEDED
    assert result.result_ref == str(planned.id)
    assert result.detail == "application is PLANNED"
    assert applications.calls == [("create", USER, OPPORTUNITY)]


@pytest.mark.asyncio
async def test_generate_resume_success_names_the_rendered_version():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=GenerateResumeAction(opportunity_id=OPPORTUNITY))
    await _seed(proposals, proposal)
    document = a_rendered_document(storage_key="k")
    documents = _StubDocuments(result=document)
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         documents=documents)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.SUCCEEDED
    assert result.result_ref == str(document.id)
    assert result.detail == "generated résumé v1 (RENDERED)"


@pytest.mark.asyncio
async def test_set_search_radius_success_describes_the_new_radius():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=SetSearchRadiusAction(search_profile_id=SEARCH_PROFILE, radius_km=42.0))
    await _seed(proposals, proposal)
    search = a_search_profile()
    onboarding = _StubOnboarding(result=search)
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         onboarding=onboarding)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.SUCCEEDED
    assert result.result_ref == str(search.id)
    assert result.detail == "radius set to 42 km"
    assert onboarding.calls == [("set_search_radius", SEARCH_PROFILE, 42.0)]


@pytest.mark.asyncio
async def test_update_search_keywords_success_describes_the_change():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=UpdateSearchKeywordsAction(
            search_profile_id=SEARCH_PROFILE, title_keywords=("python", "data")))
    await _seed(proposals, proposal)
    onboarding = _StubOnboarding(result=a_search_profile())
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         onboarding=onboarding)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.SUCCEEDED
    assert "title keywords python, data" in (result.detail or "")


# --- the exception -> outcome mapping --------------------------------------

@pytest.mark.asyncio
async def test_a_precondition_exception_is_recorded_as_rejected():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=SubmitApplicationAction(application_id=APPLICATION))
    await _seed(proposals, proposal)
    applications = _StubApplications(
        error=ApplicationNotActionable(ApplicationState.PLANNED, "submit"))
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         applications=applications)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.REJECTED
    assert result.detail == "cannot submit an application in state PLANNED"
    assert result.result_ref is None
    reloaded = await proposals.get(USER, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ChatActionProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_an_application_error_carries_its_code_into_the_result_ref():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=SubmitApplicationAction(application_id=APPLICATION))
    await _seed(proposals, proposal)
    applications = _StubApplications(
        error=ApplicationError(ApplicationFailureCode.APPLICATION_RATE_LIMITED))
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         applications=applications)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.REJECTED
    assert result.result_ref == ApplicationFailureCode.APPLICATION_RATE_LIMITED.value
    assert result.detail == "the policy's application rate limit is exhausted"


@pytest.mark.asyncio
async def test_insufficient_evidence_is_a_rejection_not_a_failure():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=GenerateResumeAction(opportunity_id=OPPORTUNITY))
    await _seed(proposals, proposal)
    documents = _StubDocuments(
        error=InsufficientEvidence("no evidence on file to build a résumé"))
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         documents=documents)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.REJECTED
    assert result.detail == "no evidence on file to build a résumé"


@pytest.mark.asyncio
async def test_an_unexpected_exception_is_failed_with_a_generic_secret_free_detail():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=SubmitApplicationAction(application_id=APPLICATION))
    await _seed(proposals, proposal)
    leaky_message = "connect failed at postgres://user:sup3rsecret@db:5432"
    applications = _StubApplications(error=RuntimeError(leaky_message))
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         applications=applications)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.FAILED
    assert result.detail == _UNEXPECTED_FAILURE_DETAIL
    assert result.result_ref is None
    assert "sup3rsecret" not in (result.detail or "")  # the raw text was dropped
    reloaded = await proposals.get(USER, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ChatActionProposalStatus.FAILED


# --- integration: the executor really consults the real validator ----------

@pytest.mark.asyncio
async def test_the_executor_honors_a_real_validator_refusal():
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=GenerateResumeAction(opportunity_id=OPPORTUNITY))
    await _seed(proposals, proposal)
    # No opportunity is seeded, so the real validator refuses OPPORTUNITY_NOT_FOUND.
    validator = ProposalValidator(
        opportunities=FakeOpportunityRepository(),
        applications=FakeApplicationRepository(),
        searches=FakeSearchProfileRepository())
    documents = _StubDocuments(error=_MUST_NOT_RUN)
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         validator=validator, documents=documents)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.REJECTED
    assert result.result_ref == ProposalRejectionCode.OPPORTUNITY_NOT_FOUND.value
    assert documents.calls == []


@pytest.mark.asyncio
async def test_the_executor_refuses_a_sibling_resource_in_an_anchored_thread():
    # §63: the thread is anchored to application A; the proposal names application B (both
    # the user's own). The executor loads the conversation and hands it to the real
    # validator, whose scope wall refuses SCOPE_MISMATCH before any ownership read — so the
    # application service is never entered and B is untouched. Defense in depth: the same
    # scope rule the creation gate applied is re-enforced here at confirm.
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=SubmitApplicationAction(application_id=OTHER_APPLICATION))
    await _seed(proposals, proposal)
    conversations = FakeConversationRepository()
    conversations.conversations[CONVERSATION] = a_conversation(
        scope=ConversationScope.APPLICATION, scope_id=APPLICATION)
    validator = ProposalValidator(
        opportunities=FakeOpportunityRepository(),
        applications=FakeApplicationRepository(),
        searches=FakeSearchProfileRepository())
    applications = _StubApplications(error=_MUST_NOT_RUN)
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         validator=validator, applications=applications,
                         conversations=conversations)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.REJECTED
    assert result.result_ref == ProposalRejectionCode.SCOPE_MISMATCH.value
    assert applications.calls == []  # B was never touched
    reloaded = await proposals.get(USER, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ChatActionProposalStatus.REJECTED


@pytest.mark.asyncio
async def test_the_executor_refuses_read_only_prep_for_a_sibling_posting():
    # §47/§11: a stale or forged proposal — the thread is anchored to opportunity A but the
    # proposal opens prep for opportunity B. Read-only or not, the executor's scope wall
    # refuses it SCOPE_MISMATCH at confirm and records no navigation hint, so a rejected
    # read-only resource action yields no client-side result. Defense in depth behind the
    # creation gate: the same scope rule is enforced again here.
    proposals = FakeChatActionProposalRepository()
    proposal = a_chat_action_proposal(
        action=OpenInterviewPrepAction(opportunity_id=OTHER_OPPORTUNITY))
    await _seed(proposals, proposal)
    conversations = FakeConversationRepository()
    conversations.conversations[CONVERSATION] = a_conversation(
        scope=ConversationScope.OPPORTUNITY, scope_id=OPPORTUNITY)
    validator = ProposalValidator(
        opportunities=FakeOpportunityRepository(),
        applications=FakeApplicationRepository(),
        searches=FakeSearchProfileRepository())
    executor = _executor(proposals=proposals,
                         executions=FakeChatActionExecutionRepository(),
                         validator=validator, conversations=conversations)

    result = await executor.execute(USER, proposal.id, now=NOW)

    assert result.outcome is ChatActionExecutionOutcome.REJECTED
    assert result.result_ref == ProposalRejectionCode.SCOPE_MISMATCH.value
    reloaded = await proposals.get(USER, proposal.id)
    assert reloaded is not None
    assert reloaded.status is ChatActionProposalStatus.REJECTED




