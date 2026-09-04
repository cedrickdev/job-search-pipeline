// Fixture payloads for the E2E suite.
//
// Deliberately separate from tests/nuxt/support/fixtures.ts: these are the
// on-the-wire JSON bodies Playwright hands the browser, and the unit builders are
// shaped for component props. Types are imported by relative path rather than the
// `~` alias, which only exists inside the Nuxt/Vite graph, not in Playwright's.
//
// No real candidate data: every company, title and URL is invented.
import type { Analytics, JobCard, JobDetail, Overview, Settings } from '../../../app/types/domain'

export const IDLE_RUN = { state: 'idle', kind: null, started_at: null, last_run: null }

export function card(overrides: Partial<JobCard> = {}): JobCard {
  return {
    application_id: 1,
    job_id: 10,
    company: 'Alpha',
    title: 'Shift Lead',
    track: 'job',
    status: 'Ready to apply',
    url: 'https://example.invalid/10',
    language: 'en',
    score: 91,
    phone_screen_pct: 88,
    ...overrides,
  }
}

export function jobDetail(overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    job: {
      id: 10,
      company: 'Alpha',
      title: 'Shift Lead',
      url: 'https://example.invalid/10',
      location: 'Lausanne',
      language: 'en',
      description: 'Retail and service.',
    },
    application: { id: 1, status: 'Ready to apply', submitted_at: null, recruiter_email: null },
    score: { score: 91, reasoning: 'Strong match' },
    fit: {
      available: true,
      coverage_score: 0.82,
      risk_tier: 'GREEN',
      matched_keywords: ['python'],
      missing_keywords: ['spark'],
    },
    cv_versions: {
      en: {
        id: 5,
        language: 'en',
        phone_screen_pct: 88,
        pdf_url: '/api/files/cv/5',
        created_at: '2026-06-11T07:00:00',
      },
      fr: null,
    },
    cover_letter: { en: null, fr: null },
    events: [
      {
        id: 1,
        event_type: 'discovered',
        detail: 'found on wtj',
        source: 'pipeline',
        created_at: '2026-06-10T09:00:00',
      },
    ],
    pending_regen: null,
    last_regen: null,
    last_apply: null,
    ...overrides,
  }
}

export function overview(overrides: Partial<Overview> = {}): Overview {
  return {
    kpis: {
      phone_screen_readiness: { value: 90, target: 90 },
      response_rate: 0.667,
      velocity: { value: 4, goal: 5 },
      in_flight: 12,
      replies_to_action: 0,
    },
    today: [card()],
    borderline: [],
    auto_approved_today: [],
    followups_due: [],
    replies_to_action: [],
    upcoming_interviews: [],
    funnel: [{ stage: 'Discovered', count: 40, pct: 1 }],
    status_breakdown: [{ status: 'Applied', count: 12 }],
    source_health: [{ source: 'LinkedIn', discovered: 30, applied: 8 }],
    ...overrides,
  }
}

export const ANALYTICS: Analytics = {
  days: 30,
  kpis: {
    phone_screen_readiness: { value: 88, target: 90 },
    response_rate: 0.4,
    velocity: { value: 6, goal: 5, window_days: 7 },
  },
  funnel: [{ stage: 'Discovered', count: 40, pct: 1 }],
  status_breakdown: [{ status: 'Applied', count: 12 }],
  applications_per_day: [{ date: '2026-06-15', count: 3 }],
  replies_per_day: [{ date: '2026-06-15', count: 2 }],
  phone_screen_trend: { target: 90, points: [{ date: '2026-06-15', value: 92, n: 2 }] },
}

export const SETTINGS: Settings = {
  auto_apply: false,
  auto_apply_min_score: 85,
  auto_apply_daily_cap: 5,
  tailor_creativity: 'balanced',
  llm_backend: 'claude_cli',
  llm_base_url: '',
  llm_model: '',
  schedule_enabled: true,
  schedule_time: '08:00',
  schedule_cadence: 'daily',
}
