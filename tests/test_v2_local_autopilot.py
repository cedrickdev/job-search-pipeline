from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.app.domain.decision import ApplicationDecisionKind
from backend.app.domain.documents import CandidateDocumentType, DocumentStatus
from backend.app.services.local_autopilot import LocalAutopilotService


NOW = datetime(2026, 10, 5, 8, tzinfo=UTC)
USER = "11111111-1111-4111-8111-111111111111"
OPPORTUNITY = "22222222-2222-4222-8222-222222222222"


def usable_document(document_type: CandidateDocumentType):
    return SimpleNamespace(
        document_type=document_type,
        versions=(SimpleNamespace(status=DocumentStatus.RENDERED),),
    )


class FakeAssessment:
    def __init__(self, *, score: float = 0.9):
        self.match = SimpleNamespace(overall=score)
        self.eligibility = SimpleNamespace(status="ELIGIBLE")


class FakePipeline:
    def __init__(self, *, decision_kind=ApplicationDecisionKind.AUTO_APPLY,
                 document_status=DocumentStatus.RENDERED,
                 prepared_state="APPROVED"):
        self.calls = []
        self.decision_kind = decision_kind
        self.document_status = document_status
        self.prepared_state = prepared_state

    async def evaluate(self, user_id, opportunity_id, *, now):
        self.calls.append(("assess", user_id, opportunity_id))
        return FakeAssessment()

    async def decide(self, user_id, opportunity_id, *, now):
        self.calls.append(("decide", user_id, opportunity_id))
        return SimpleNamespace(
            kind=self.decision_kind,
            requires_human_review=self.decision_kind is ApplicationDecisionKind.REQUIRE_REVIEW,
            reasons=(SimpleNamespace(code="MEETS_POLICY", detail="approved"),),
        )

    async def create(self, user_id, opportunity_id, *, now):
        self.calls.append(("create", user_id, opportunity_id))
        return SimpleNamespace(id="app-1", state="PLANNED")

    async def generate(self, user_id, opportunity_id, document_type, *, now, language=None):
        self.calls.append(("generate", document_type))
        return SimpleNamespace(
            document_type=document_type,
            versions=(SimpleNamespace(status=self.document_status),),
        )

    async def prepare(self, user_id, application_id, *, now):
        self.calls.append(("prepare", application_id))
        return SimpleNamespace(id=application_id, state=self.prepared_state)


def service(fake: FakePipeline) -> LocalAutopilotService:
    return LocalAutopilotService(
        assessor=fake, decider=fake, applications=fake, documents=fake)


@pytest.mark.asyncio
async def test_local_run_assesses_generates_both_documents_and_prepares_without_queueing():
    fake = FakePipeline()
    report = await service(fake).run(USER, OPPORTUNITY, now=NOW, language="fr")

    assert [call[0] for call in fake.calls] == [
        "assess", "decide", "create", "generate", "generate", "prepare"]
    assert report.application.state == "APPROVED"
    assert report.next_action == "QUEUE_BROWSER_SUBMISSION"
    assert report.stop_reason is None


@pytest.mark.asyncio
async def test_local_run_stops_before_documents_when_policy_decision_is_not_actionable():
    fake = FakePipeline(decision_kind=ApplicationDecisionKind.SKIP)
    report = await service(fake).run(USER, OPPORTUNITY, now=NOW)

    assert [call[0] for call in fake.calls] == ["assess", "decide"]
    assert report.application is None
    assert report.stop_reason == "DECISION_NOT_ACTIONABLE"


@pytest.mark.asyncio
async def test_local_run_stops_before_prepare_when_a_document_fails_the_guard():
    fake = FakePipeline(document_status=DocumentStatus.REJECTED)
    report = await service(fake).run(USER, OPPORTUNITY, now=NOW)

    assert [call[0] for call in fake.calls] == [
        "assess", "decide", "create", "generate", "generate"]
    assert report.application is not None
    assert report.stop_reason == "DOCUMENT_NOT_USABLE"
    assert report.next_action == "REVIEW_DOCUMENT"
