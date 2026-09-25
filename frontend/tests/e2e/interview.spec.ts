// The interview simulator in a real browser: reachable from the nav, the open session and
// its current question on show, and — the phase's whole point, proven where a person would
// see it — a graded answer earns coaching, never a forecast.
//
// The unit suite (tests/nuxt/pages/interview.spec.ts) mounts this page against a stubbed
// `fetch`; here the navigation and the submit are real. Nothing leaves the machine: every
// `/api/**` request is answered from the table below, so no live board, transcriber or LLM
// is reached (CLAUDE.md §Testing). Readiness is its own card, taken from the session's
// readiness endpoint the platform computes — the page renders no probability or verdict.
import { expect, test } from '@playwright/test'
import { mockApi } from './support/api'
import { IDLE_RUN, overview } from './support/fixtures'
import type { MockRoute } from './support/api'

const ACCOUNT = {
  id: '11111111-1111-4111-8111-111111111111',
  email: 'candidate@example.invalid',
  display_name: 'Test Candidate',
  status: 'ACTIVE',
  created_at: '2026-01-02T09:00:00Z',
  onboarding_completed_at: '2026-01-02T09:30:00Z',
}
const SESSION_WINDOW = {
  issued_at: '2026-01-02T09:00:00Z',
  expires_at: '2026-01-09T09:00:00Z',
  last_seen_at: '2026-01-02T09:15:00Z',
}

const PROFILE_ID = '22222222-2222-4222-8222-222222222222'
const OPPORTUNITY_ID = '55555555-5555-4555-8555-555555555555'
const SESSION_ID = '1a1a1a1a-1a1a-4a1a-8a1a-1a1a1a1a1a1a'
const QUESTION_ID = '2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b'
const ANSWER_ID = '3c3c3c3c-3c3c-4c3c-8c3c-3c3c3c3c3c3c'
const EVALUATION_ID = '4d4d4d4d-4d4d-4d4d-8d4d-4d4d4d4d4d4d'

const PROFILE = { id: PROFILE_ID }

const PLAN = {
  mode: 'BEHAVIORAL',
  target_question_count: 4,
  topics: [{ label: 'Ownership', question_type: 'BEHAVIORAL', target_questions: 2 }],
}
const SESSION = {
  id: SESSION_ID,
  candidate_profile_id: PROFILE_ID,
  opportunity_id: OPPORTUNITY_ID,
  application_id: null,
  title: 'Behavioral practice · Backend Engineer',
  mode: 'BEHAVIORAL',
  style: 'COACHING',
  difficulty: 'INTERMEDIATE',
  language: null,
  status: 'IN_PROGRESS',
  is_active: true,
  plan: PLAN,
  ended_at: null,
  created_at: '2026-03-02T09:00:00Z',
  updated_at: '2026-03-02T09:05:00Z',
}
const QUESTION = {
  id: QUESTION_ID,
  session_id: SESSION_ID,
  sequence: 0,
  depth: 0,
  question_type: 'BEHAVIORAL',
  difficulty: 'INTERMEDIATE',
  prompt: 'Tell me about a time you took ownership of a problem no one else would.',
  topic_label: 'Ownership',
  is_follow_up: false,
  follows_sequence: null,
  generator_key: 'deterministic-interview/1',
  asked_at: '2026-03-02T09:05:00Z',
}
const READINESS = {
  overall: null,
  overall_percent: null,
  band: 'UNKNOWN',
  dimensions: [
    { dimension: 'CLARITY', mean_score: null, evaluated_count: 0, weight: 0.15 },
    { dimension: 'RELEVANCE', mean_score: null, evaluated_count: 0, weight: 0.2 },
    { dimension: 'COMPLETENESS', mean_score: null, evaluated_count: 0, weight: 0.2 },
    { dimension: 'STRUCTURE', mean_score: null, evaluated_count: 0, weight: 0.25 },
    { dimension: 'SPECIFICITY', mean_score: null, evaluated_count: 0, weight: 0.2 },
  ],
  coverage: 0,
  answered_questions: 0,
  evaluated_answers: 0,
  profile_version: 'interview-readiness/1.0',
  computed_at: '2026-03-02T09:05:00Z',
}
const DETAIL = {
  session: SESSION,
  questions: [QUESTION],
  answers: [],
  evaluations: [],
  summary: null,
}

const ANSWER = {
  id: ANSWER_ID,
  session_id: SESSION_ID,
  question_id: QUESTION_ID,
  format: 'TEXT',
  content: 'I owned the flaky-deploy fix end to end.',
  transcript_confidence: null,
  answered_at: '2026-03-02T09:06:00Z',
}
const EVALUATION = {
  id: EVALUATION_ID,
  session_id: SESSION_ID,
  answer_id: ANSWER_ID,
  dimensions: [
    { dimension: 'CLARITY', status: 'EVALUATED', score: 0.8, notes: ['Clear and direct.'] },
    { dimension: 'STRUCTURE', status: 'EVALUATED', score: 0.6, notes: ['Lean on STAR.'] },
  ],
  strengths: ['Owned a concrete outcome end to end.'],
  improvements: ['Quantify the impact you had.'],
  suggested_answer: 'When our deploys were flaky, I took the fix end to end and…',
  confidence: 0.7,
  evaluator_key: 'deterministic-interview/1',
  evaluated_at: '2026-03-02T09:06:30Z',
}
const OUTCOME = { session: SESSION, answer: ANSWER, evaluation: EVALUATION }

/**
 * A signed-in account, the reads the interview page makes, and the V1 shell reads the app
 * makes on any screen — an unstubbed one lands in `unmatched` and fails the hermeticity
 * check. The per-session reads (`/history`, `/detail`, `/readiness`, `/answers`) precede the
 * bare `/interview-sessions` list because every one of their URLs contains that substring;
 * first match wins.
 */
function routes(...first: MockRoute[]): MockRoute[] {
  return [
    ...first,
    { match: '/api/v2/auth/session', method: 'GET', json: { account: ACCOUNT, session: SESSION_WINDOW } },
    { match: '/api/v2/me/profile', method: 'GET', json: PROFILE },
    { match: '/interview-sessions/history', method: 'GET', json: { summaries: [] } },
    { match: '/detail', method: 'GET', json: DETAIL },
    { match: '/readiness', method: 'GET', json: READINESS },
    { match: '/api/v2/applications', method: 'GET', json: { applications: [] } },
    { match: '/api/v2/interview-sessions', method: 'GET', json: { sessions: [SESSION] } },
    { match: '/api/overview', json: overview() },
    { match: '/api/runs/status', json: IDLE_RUN },
    { match: '/api/chat/history', json: { messages: [] } },
  ]
}

test('reaches interview practice from the nav and shows the open session and its question', async ({ page }) => {
  const api = await mockApi(page, routes())
  await page.goto('/')

  await page.getByRole('link', { name: 'Interview' }).click()
  await expect(page).toHaveURL('/interview')
  await expect(page.getByRole('heading', { name: 'Interview practice' })).toBeVisible()
  // The title appears twice — a button in the session list and the open panel's
  // heading — so scope to the heading rather than a bare text match.
  await expect(page.getByRole('heading', { name: 'Behavioral practice · Backend Engineer' })).toBeVisible()
  await expect(page.getByText('Tell me about a time you took ownership')).toBeVisible()
  // Readiness is a coaching signal the platform computes, and says so — not a forecast.
  await expect(page.getByText('not a hiring forecast')).toBeVisible()
  await expect(page.locator('body')).not.toContainText(/probability|verdict|likelihood/i)
  expect(api.unmatched).toEqual([])
})

test('grades a typed answer into coaching, never a forecast', async ({ page }) => {
  await mockApi(page, routes(
    { match: '/answers', method: 'POST', json: OUTCOME },
  ))
  await page.goto('/interview')

  await page.getByLabel('Your answer').fill('I owned the flaky-deploy fix end to end.')
  await page.getByRole('button', { name: 'Submit answer' }).click()

  // The answer earned coaching in place: strengths and what to improve, no probability.
  await expect(page.getByRole('heading', { name: 'Coaching' })).toBeVisible()
  await expect(page.getByText('Owned a concrete outcome end to end.')).toBeVisible()
  await expect(page.getByText('Quantify the impact you had.')).toBeVisible()
  await expect(page.getByText('not a hiring forecast')).toBeVisible()
  await expect(page.locator('body')).not.toContainText(/probability|verdict|likelihood/i)
})
