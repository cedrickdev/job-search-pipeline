"""The Phase 14 interview simulator's turn machinery, asserted against PostgreSQL.

The flow suite (`test_v2_interview_service.py`) proves the lifecycle logic over fakes; the
persistence suite (round-trips, the provenance SET NULL) proves the schema. This proves the
one property only a real database, under real concurrency, establishes: the derived-id
writes the engine leans on are safe when two of a user's own clients race the same turn.

Two races matter, and each resolves through a deterministic id colliding on its primary key:

- **Two answers to the same current question.** Both submissions pass the read-then-check
  (`get_for_question` is `None` for both), then race to insert an answer whose id derives from
  the question — so the loser's flush raises an `IntegrityError` the service catches and
  re-raises as `QUESTION_ALREADY_ANSWERED`, never a raw error and never overwriting the
  winner's immutable answer (§41-43).
- **Two `next_question` calls advancing one session.** On an `IN_PROGRESS` session whose current
  question is already answered, both calls skip the idempotent replay, take no session-row lock,
  and independently ask the same next primary, whose id derives from `(session_id, sequence)` —
  so the loser collides on that id and is refused `INTERVIEW_STATE_CONFLICT`, never a raw error
  and never a duplicated question at one sequence. A barrier inside the LLM double makes both
  callers meet past their reads and before their insert, so the collision is deterministic.

Neither can be shown over `db_session` (one savepointed connection cannot demonstrate a
cross-connection race) nor over fakes (whose in-memory dict has no unique index), so — exactly
as `test_v2_persistence_application_engine.py` does for the submission budget — these commit
for real over independent `session_scope` units of work and truncate on the way out. The LLM
is the deterministic `FakeInterviewLLM`; no provider, no transcriber audio, no live call.
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from backend.app.domain.identifiers import interview_question_id
from backend.app.domain.interview import (
    InterviewErrorCode,
    InterviewSessionStatus,
)
from backend.app.infrastructure.database.engine import (
    create_session_factory,
    session_scope,
)
from backend.app.infrastructure.database.models import (
    InterviewAnswerRow,
    InterviewQuestionRow,
)
from backend.app.interview.context import InterviewContextBuilder
from backend.app.interview.llm import InterviewLLMResult, ProposedQuestion
from backend.app.interview.service import (
    AnswerOutcome,
    InterviewError,
    InterviewService,
    InterviewTurn,
)
from backend.app.interview.transcriber import DeterministicTranscriber
from backend.app.repositories.sqlalchemy_repositories import (
    SqlAlchemyApplicationRepository,
    SqlAlchemyCandidateProfileRepository,
    SqlAlchemyInterviewAnswerEvaluationRepository,
    SqlAlchemyInterviewAnswerRepository,
    SqlAlchemyInterviewQuestionRepository,
    SqlAlchemyInterviewSessionRepository,
    SqlAlchemyInterviewSessionSummaryRepository,
    SqlAlchemyOpportunityRepository,
)
from tests.v2_builders import (
    LATER,
    SESSION,
    USER,
    a_candidate_profile,
    an_interview_answer,
    an_interview_question,
    an_interview_session,
    an_opportunity,
)
from tests.v2_interview import FakeInterviewLLM
from tests.v2_rows import a_user_row

# APPEND-MARKER


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def committed_world(db_engine):
    """A real session factory whose committed rows are truncated on teardown.

    A cross-connection race can only be shown across two units of work that see each other's
    committed writes, so these tests cannot lean on `db_session`'s rollback. This commits for
    real and, on the way out, truncates `users` and `opportunities` with CASCADE — reaching
    every account- and posting-scoped row the tests wrote — so the once-per-session schema is
    left clean for whatever runs next.
    """
    factory = create_session_factory(db_engine)
    try:
        yield factory
    finally:
        async with db_engine.begin() as connection:
            await connection.execute(text(
                "TRUNCATE users, opportunities RESTART IDENTITY CASCADE"))


def _service(session, *, llm: FakeInterviewLLM | None = None) -> InterviewService:
    """An `InterviewService` wired to real repositories over one session and a fake LLM.

    Every repository is the real SQLAlchemy one so the derived-id collisions this file exists
    to exercise actually reach the database; only the provider round trip is the deterministic
    `FakeInterviewLLM`, so no live call is made. The transcriber is unused (the tests submit
    text), and the engine and guard default to the real, stateless ones. `llm` lets the
    next-question race share one rendezvous double across both callers; it defaults to a fresh,
    unsynchronised fake.
    """
    return InterviewService(
        sessions=SqlAlchemyInterviewSessionRepository(session),
        questions=SqlAlchemyInterviewQuestionRepository(session),
        answers=SqlAlchemyInterviewAnswerRepository(session),
        evaluations=SqlAlchemyInterviewAnswerEvaluationRepository(session),
        summaries=SqlAlchemyInterviewSessionSummaryRepository(session),
        applications=SqlAlchemyApplicationRepository(session),
        context=InterviewContextBuilder(
            profiles=SqlAlchemyCandidateProfileRepository(session),
            opportunities=SqlAlchemyOpportunityRepository(session)),
        llm=llm if llm is not None else FakeInterviewLLM(),
        transcriber=DeterministicTranscriber())


class _RendezvousInterviewLLM(FakeInterviewLLM):
    """A `FakeInterviewLLM` that makes two `generate_question` callers meet before either writes.

    The next-question race only bites if both callers pass their derived-id read (finding it
    absent) before *either* commits its insert. Run untethered over a fast local database, the
    first caller runs its whole unit of work — context reads, insert, commit — before the second
    reaches its load-first read, so the second finds the committed row and its upsert becomes a
    harmless update instead of a colliding insert: two questions "asked", no conflict.

    So both callers rendezvous on an `asyncio.Barrier` *inside* `generate_question` — the step
    the service runs after building context and before `_questions.upsert` (see
    `_generate_question`). Neither returns from the barrier until both have arrived, so both are
    past their reads and about to insert the same `(session, sequence)` id when released. One
    insert lands; the other collides on the primary key and is caught as
    `INTERVIEW_STATE_CONFLICT`. The wait is bounded so a caller that never arrives (a diverging
    path) fails the test loudly instead of hanging it.
    """

    def __init__(self, barrier: asyncio.Barrier, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._barrier = barrier

    async def generate_question(self, **kwargs: object) -> InterviewLLMResult[ProposedQuestion]:
        await asyncio.wait_for(self._barrier.wait(), timeout=10.0)
        return await super().generate_question(**kwargs)  # type: ignore[arg-type]


async def _seed_grounding(session) -> None:
    """Commit the account, its profile and the posting a session is grounded in."""
    session.add(a_user_row(display_name="owner"))
    await session.flush()
    await SqlAlchemyCandidateProfileRepository(session).upsert(a_candidate_profile())
    await SqlAlchemyOpportunityRepository(session).upsert(an_opportunity())


async def _count(factory, model) -> int:
    async with session_scope(factory, commit=False) as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


async def test_two_answers_to_the_same_question_store_one_and_refuse_the_other(
        committed_world):
    """Two clients answering the current question at once: one lands, one is refused (§41-43).

    Both pass the read-then-check, then race to insert an answer whose id derives from the
    question. The loser's flush hits the unique key and is caught and re-raised as
    `QUESTION_ALREADY_ANSWERED` — never a raw `IntegrityError`, and the winner's immutable
    answer is never overwritten. Exactly one answer row exists at the end.
    """
    async with session_scope(committed_world) as session:
        await _seed_grounding(session)
        await SqlAlchemyInterviewSessionRepository(session).upsert(
            an_interview_session(status=InterviewSessionStatus.IN_PROGRESS))
        await SqlAlchemyInterviewQuestionRepository(session).upsert(
            an_interview_question())

    async def _answer(content: str):
        async with session_scope(committed_world) as session:
            return await _service(session).submit_text_answer(
                USER, SESSION, content, now=LATER)

    results = await asyncio.gather(
        _answer("Ma première réponse structurée."),
        _answer("Une seconde réponse, en parallèle."),
        return_exceptions=True)

    stored = [r for r in results if isinstance(r, AnswerOutcome)]
    refused = [r for r in results
               if isinstance(r, InterviewError)
               and r.code is InterviewErrorCode.QUESTION_ALREADY_ANSWERED]
    assert len(stored) == 1, results
    assert len(refused) == 1, results
    # The database holds exactly one answer — the loser wrote nothing.
    assert await _count(committed_world, InterviewAnswerRow) == 1


async def test_two_next_question_calls_ask_one_and_conflict_the_other(committed_world):
    """Two clients asking the next question at once: one asks, one conflicts (§41-43).

    On an `IN_PROGRESS` session whose current question is already answered, both calls skip the
    idempotent replay, take no session-row lock (the session is past `CREATED`, so neither
    advances), and independently compute the same next primary — whose id derives from
    `(session_id, sequence)`. Both then race to insert it; the loser collides on that derived id
    and is refused `INTERVIEW_STATE_CONFLICT`, never a raw error and never a second question at
    one sequence. Exactly one new question (sequence 1) is recorded.

    The seeded answer carries no evaluation, so `_next_request` finds none to drill and both
    calls fall through to the next planned topic — a deterministic primary, no follow-up
    decision — which keeps the race about the id collision and nothing else.

    A shared `_RendezvousInterviewLLM` makes the collision deterministic rather than
    timing-dependent: both callers meet on a barrier inside `generate_question`, past their
    load-first reads and about to insert, so the loser genuinely collides on the derived id
    instead of quietly updating a row the winner already committed.
    """
    async with session_scope(committed_world) as session:
        await _seed_grounding(session)
        await SqlAlchemyInterviewSessionRepository(session).upsert(
            an_interview_session(status=InterviewSessionStatus.IN_PROGRESS))
        await SqlAlchemyInterviewQuestionRepository(session).upsert(
            an_interview_question())
        await SqlAlchemyInterviewAnswerRepository(session).upsert(an_interview_answer())

    llm = _RendezvousInterviewLLM(asyncio.Barrier(2))

    async def _next():
        async with session_scope(committed_world) as session:
            return await _service(session, llm=llm).next_question(USER, SESSION, now=LATER)

    results = await asyncio.gather(_next(), _next(), return_exceptions=True)

    asked = [r for r in results
             if isinstance(r, InterviewTurn) and r.question is not None]
    conflicted = [r for r in results
                  if isinstance(r, InterviewError)
                  and r.code is InterviewErrorCode.INTERVIEW_STATE_CONFLICT]
    assert len(asked) == 1, results
    assert len(conflicted) == 1, results
    # Exactly one new question at sequence 1 was recorded (plus the seeded sequence 0) — the
    # loser duplicated nothing.
    assert await _count(committed_world, InterviewQuestionRow) == 2
    winning = asked[0].question
    assert winning is not None
    assert winning.id == interview_question_id(SESSION, 1)


