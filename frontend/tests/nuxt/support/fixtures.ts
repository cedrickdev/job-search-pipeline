// Fixture builders for the unit suite.
//
// V1 spelled every fixture out inline in each test file. The response shapes have
// enough required fields (`JobCard` has eleven) that repeating them per file
// buries the one or two values a test actually cares about, so the shared parts
// live here and each test overrides only what it asserts on.
//
// No real candidate data: companies and titles are invented, as in V1's fixtures.
import type { FollowupItem, JobCard, Overview, PrepData } from '~/types/domain'

export function card(overrides: Partial<JobCard> = {}): JobCard {
  return {
    application_id: 1,
    job_id: 10,
    company: 'Alpha',
    title: 'Shift Lead',
    track: 'job',
    status: 'Ready to apply',
    url: 'https://example.invalid/1',
    language: 'en',
    score: 91,
    phone_screen_pct: 88,
    ...overrides,
  }
}

export function followup(overrides: Partial<FollowupItem> = {}): FollowupItem {
  return {
    kind: 'applied_no_reply',
    application_id: 3,
    job_id: 103,
    company: 'Initech',
    title: 'Analyst',
    days: 9,
    since: '2026-06-07',
    ...overrides,
  }
}

export function prep(overrides: Partial<PrepData> = {}): PrepData {
  return {
    notes_md: '',
    likely_questions: null,
    company_research: null,
    talking_points: null,
    generated_at: null,
    interviews: [],
    ...overrides,
  }
}

/** An overview with every list empty — tests fill in the slice they exercise. */
export function overview(overrides: Partial<Overview> = {}): Overview {
  return {
    kpis: {
      phone_screen_readiness: { value: null, target: 80 },
      response_rate: null,
      velocity: { value: 0, goal: 5 },
      in_flight: 0,
      replies_to_action: 0,
    },
    today: [],
    borderline: [],
    auto_approved_today: [],
    followups_due: [],
    replies_to_action: [],
    upcoming_interviews: [],
    funnel: [],
    status_breakdown: [],
    source_health: [],
    ...overrides,
  }
}
