// The interview-simulator store: the client half of "practice, never prediction".
//
// Written against the real `fetch` stub, like the chat store's spec, because what matters is
// what the store does with what the server returned. Two behaviours carry the phase's rule.
// Readiness is never taken from a submit or a summary the store assembled — it is fetched
// from its own endpoint after every grade, and a completed session's readiness is the
// summary's computed one. And an answer's coaching is only coaching: the evaluation the
// store holds has no probability, verdict or readiness to render, so a degraded submit
// leaves `lastEvaluation` null rather than inventing one.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useInterviewStore } from '~/stores/interview'
import { stubFetch } from '../support/http'
import {
  interviewAnswer,
  interviewDetail,
  interviewEvaluation,
  interviewOutcome,
  interviewQuestion,
  interviewSession,
  interviewSessionList,
  interviewSummary,
  interviewSummaryList,
  interviewTurn,
  profile,
  sessionReadiness,
} from '../support/v2-fixtures'
import type { Route } from '../support/http'

const SESSIONS = '/api/v2/interview-sessions'
const SESSION_ID = '1a1a1a1a-1a1a-4a1a-8a1a-1a1a1a1a1a1a'
const QUESTION_ID = '2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b'
const QUESTION_ID_2 = '2b2b2b2b-2b2b-4b2b-8b2b-000000000002'
const PROFILE_ID = '22222222-2222-4222-8222-222222222222'
const OPPORTUNITY_ID = '55555555-5555-4555-8555-555555555555'

function store() {
  return useInterviewStore()
}

// APPEND-MARKER

/**
 * The interview routes, most specific first so `stubFetch`'s first-match wins the
 * collisions (every URL under `/interview-sessions` contains that substring, and
 * `/voice-answers` must precede `/answers`). Prepend `overrides` to shape one case.
 */
function interviewRoutes(overrides: Route[] = []): Route[] {
  return [
    ...overrides,
    { match: '/me/profile', method: 'GET', json: profile() },
    { match: '/interview-sessions/history', method: 'GET', json: interviewSummaryList() },
    { match: '/detail', method: 'GET', json: interviewDetail() },
    { match: '/readiness', method: 'GET', json: sessionReadiness() },
    { match: '/next-question', method: 'POST', json: interviewTurn() },
    {
      match: '/voice-answers',
      method: 'POST',
      json: interviewOutcome({
        answer: interviewAnswer({
          format: 'VOICE', transcript_confidence: 0.8, content: 'Spoken answer.' }),
      }),
    },
    { match: '/answers', method: 'POST', json: interviewOutcome() },
    { match: '/evaluate', method: 'POST', json: interviewEvaluation() },
    { match: '/complete', method: 'POST', json: interviewSummary() },
    { match: '/abandon', method: 'POST', json: interviewSession({ status: 'ABANDONED', is_active: false }) },
    { match: SESSIONS, method: 'POST', json: interviewSession() },
    { match: SESSIONS, method: 'GET', json: interviewSessionList() },
  ]
}

describe('interview store', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    store().$reset()
  })

  // LOAD-TESTS
  it('loads this account\'s sessions and readiness history', async () => {
    stubFetch(interviewRoutes())
    const s = store()
    await s.loadSessions()
    await s.loadHistory()
    expect(s.sessions.map(session => session.id)).toEqual([SESSION_ID])
    expect(s.history.map(summary => summary.session_id)).toEqual([SESSION_ID])
  })

  it('resolves the profile id a new session grounds on', async () => {
    stubFetch(interviewRoutes())
    const s = store()
    await s.loadProfile()
    expect(s.profileId).toBe(PROFILE_ID)
  })

  it('leaves the profile id null and stays silent when none is saved', async () => {
    stubFetch(interviewRoutes([
      { match: '/me/profile', method: 'GET', status: 404, json: { error: 'candidate_profile_not_found' } },
    ]))
    const s = store()
    await s.loadProfile()
    expect(s.profileId).toBeNull()
    expect(s.error).toBeNull()
  })

  it('records the refusal sentence when loading sessions fails', async () => {
    stubFetch([{ match: SESSIONS, method: 'GET', status: 503, json: { error: 'database_unavailable' } }])
    const s = store()
    await s.loadSessions()
    expect(s.error).toBe('The service is temporarily unavailable. Try again in a moment.')
  })

  // CREATE-TESTS
  it('opens a new session at the top and makes it active', async () => {
    const http = stubFetch(interviewRoutes())
    const s = store()
    await s.loadProfile()
    const id = await s.create(OPPORTUNITY_ID, 'BEHAVIORAL')
    expect(id).toBe(SESSION_ID)
    expect(s.activeId).toBe(SESSION_ID)
    expect(s.sessions[0]?.id).toBe(SESSION_ID)
    expect(s.question).toBeNull()
    // The owner is never in the body — the account is the session's; the profile and
    // opportunity are.
    expect(http.bodyOf('/interview-sessions')).toMatchObject({
      candidate_profile_id: PROFILE_ID,
      opportunity_id: OPPORTUNITY_ID,
      mode: 'BEHAVIORAL',
    })
  })

  it('refuses to create without a profile and posts nothing', async () => {
    const http = stubFetch(interviewRoutes())
    const s = store()
    const id = await s.create(OPPORTUNITY_ID, 'BEHAVIORAL')
    expect(id).toBeNull()
    expect(s.error).toContain('Save a profile')
    expect(http.callsTo('/interview-sessions').filter(c => c.method === 'POST')).toHaveLength(0)
  })

  // SELECT-TESTS
  it('opens a session: detail, the current question, and platform readiness', async () => {
    stubFetch(interviewRoutes())
    const s = store()
    await s.select(SESSION_ID)
    expect(s.activeId).toBe(SESSION_ID)
    expect(s.session?.status).toBe('IN_PROGRESS')
    expect(s.question?.id).toBe(QUESTION_ID)
    expect(s.lastAnswer).toBeNull()
    expect(s.readiness?.band).toBe('UNKNOWN')
  })

  it('derives the current question as the one with no answer yet', async () => {
    const q0 = interviewQuestion({ sequence: 0 })
    const q1 = interviewQuestion({ id: QUESTION_ID_2, sequence: 1, prompt: 'And a follow-up?' })
    const answer = interviewAnswer({ question_id: q0.id })
    const evaluation = interviewEvaluation({ answer_id: answer.id })
    stubFetch(interviewRoutes([
      {
        match: '/detail',
        method: 'GET',
        json: interviewDetail({
          questions: [q0, q1], answers: [answer], evaluations: [evaluation] }),
      },
    ]))
    const s = store()
    await s.select(SESSION_ID)
    expect(s.question?.id).toBe(QUESTION_ID_2)
    expect(s.lastQuestion?.id).toBe(q0.id)
    expect(s.lastAnswer?.id).toBe(answer.id)
    expect(s.lastEvaluation?.id).toBe(evaluation.id)
  })

  // LOOP-TESTS
  it('asks the next question and starts the session', async () => {
    stubFetch(interviewRoutes())
    const s = store()
    s.activeId = SESSION_ID
    await s.nextQuestion()
    expect(s.question?.id).toBe(QUESTION_ID)
    expect(s.session?.status).toBe('IN_PROGRESS')
  })

  it('submits a typed answer, records its coaching, and refreshes readiness', async () => {
    const http = stubFetch(interviewRoutes())
    const s = store()
    s.activeId = SESSION_ID
    s.question = interviewQuestion()
    await s.submitText('A structured, concrete answer.')
    expect(s.lastAnswer?.format).toBe('TEXT')
    expect(s.lastEvaluation?.id).toBe(interviewEvaluation().id)
    expect(s.question).toBeNull()
    // Readiness came from its own endpoint after the grade, never from the submit body.
    expect(http.callsTo('/readiness')).toHaveLength(1)
  })

  it('carries no hiring forecast on an answer\'s coaching', async () => {
    stubFetch(interviewRoutes())
    const s = store()
    s.activeId = SESSION_ID
    s.question = interviewQuestion()
    await s.submitText('An answer.')
    const evaluation = s.lastEvaluation as Record<string, unknown>
    expect('probability' in evaluation).toBe(false)
    expect('verdict' in evaluation).toBe(false)
    expect('readiness' in evaluation).toBe(false)
  })

  it('leaves coaching null when a submit degrades, and does not send an empty answer', async () => {
    const http = stubFetch(interviewRoutes([
      { match: '/answers', method: 'POST', json: interviewOutcome({ evaluation: null }) },
    ]))
    const s = store()
    s.activeId = SESSION_ID
    s.question = interviewQuestion()
    await s.submitText('   ')
    expect(http.callsTo('/answers')).toHaveLength(0)
    await s.submitText('A real answer.')
    expect(s.lastAnswer).not.toBeNull()
    expect(s.lastEvaluation).toBeNull()
  })

  it('transcribes and grades a spoken answer, recording it as VOICE', async () => {
    const http = stubFetch(interviewRoutes())
    const s = store()
    s.activeId = SESSION_ID
    s.question = interviewQuestion()
    await s.submitVoice(new Blob(['clip'], { type: 'audio/webm' }))
    expect(s.lastAnswer?.format).toBe('VOICE')
    expect(s.lastAnswer?.transcript_confidence).toBe(0.8)
    expect(http.callsTo('/voice-answers')).toHaveLength(1)
  })

  it('strictly re-grades the last answered question and surfaces its coaching', async () => {
    const http = stubFetch(interviewRoutes())
    const s = store()
    s.activeId = SESSION_ID
    s.lastQuestion = interviewQuestion({ sequence: 3 })
    await s.evaluate()
    expect(s.lastEvaluation?.id).toBe(interviewEvaluation().id)
    expect(http.callsTo('/questions/3/evaluate')).toHaveLength(1)
  })

  // CLOSE-TESTS



})

