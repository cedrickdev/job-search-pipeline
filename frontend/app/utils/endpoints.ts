// The compile-time link between this app and FastAPI.
//
// `paths` comes from app/types/api.d.ts, which openapi-typescript generates from
// openapi.json, which scripts/dump_openapi.py dumps out of the FastAPI app
// itself. So `keyof paths` is the exact set of routes the backend serves.
//
// Writing every URL template here and constraining it with
// `satisfies Record<string, keyof paths>` turns a backend rename or removal into
// a `nuxt typecheck` failure instead of a runtime 404. Path parameters are
// interpolated by `job()` below, which takes its template from this table — a
// literal `/api/jobs/${id}` written inline anywhere else would escape the check,
// so it should not be.
//
// The table lists all 28 paths, including the three the migrated UI never calls
// (`approved`, `jobFit`, `interview`) and the one whose URL the backend hands us
// ready-made (`cvFile`, arriving as `CvVersion.pdf_url`). Listing them costs
// nothing and keeps the guard covering the whole surface rather than the subset
// that happens to have a caller today.
//
// `V2_ENDPOINTS`, at the bottom, is the same idea for `/api/v2`. It is a second
// table because the two surfaces answer to different rules; see the comment there.
import type { paths } from '~/types/api'

export const ENDPOINTS = {
  analytics: '/api/analytics',
  approved: '/api/approved',
  chat: '/api/chat',
  chatHistory: '/api/chat/history',
  cvFile: '/api/files/cv/{cv_id}',
  interview: '/api/interviews/{interview_id}',
  jobs: '/api/jobs',
  job: '/api/jobs/{job_id}',
  jobApplied: '/api/jobs/{job_id}/applied',
  jobApplyNow: '/api/jobs/{job_id}/apply-now',
  jobDraftFollowup: '/api/jobs/{job_id}/draft_followup',
  jobFit: '/api/jobs/{job_id}/fit',
  jobFollowupDismiss: '/api/jobs/{job_id}/followup/dismiss',
  jobFollowupSnooze: '/api/jobs/{job_id}/followup/snooze',
  jobGo: '/api/jobs/{job_id}/go',
  jobInterviews: '/api/jobs/{job_id}/interviews',
  jobPrep: '/api/jobs/{job_id}/prep',
  jobPrepGenerate: '/api/jobs/{job_id}/prep/generate',
  jobPrepNotes: '/api/jobs/{job_id}/prep/notes',
  jobRegen: '/api/jobs/{job_id}/regen',
  jobSkip: '/api/jobs/{job_id}/skip',
  jobStatus: '/api/jobs/{job_id}/status',
  overview: '/api/overview',
  runsDiscover: '/api/runs/discover',
  runsFull: '/api/runs/full',
  runsStatus: '/api/runs/status',
  settings: '/api/settings',
  transcribe: '/api/transcribe',
} as const satisfies Record<string, keyof paths>

/** Every endpoint whose template contains `{job_id}`. */
type JobTemplate = Extract<(typeof ENDPOINTS)[keyof typeof ENDPOINTS], `${string}{job_id}${string}`>

/** Resolve a `{job_id}` template against a concrete job id. */
export function job(template: JobTemplate, jobId: number): string {
  return template.replace('{job_id}', String(jobId))
}

// The V2 surface, kept as a separate table rather than folded into the one above.
//
// Two reasons, and both are about telling a reader something true. The V1 table is
// closed — 28 paths, none of which will be added to — while this one grows with
// every V2 phase. And the two surfaces have different rules: a V1 request needs no
// session and no CSRF header, every one of these except register and login answers
// 401 without a session cookie, and every unsafe one needs `X-CSRF-Token`
// (docs/AUTHENTICATION.md). One merged table would hide that split behind an
// alphabetical list.
//
// Twelve operations over these nine paths: `/me/profile` has a GET and a PUT,
// `/me/search-profiles` a GET and a POST, and `{search_profile_id}` a PUT and a
// DELETE. The `satisfies` clause is the same compile-time guard.
export const V2_ENDPOINTS = {
  login: '/api/v2/auth/login',
  logout: '/api/v2/auth/logout',
  register: '/api/v2/auth/register',
  session: '/api/v2/auth/session',
  profile: '/api/v2/me/profile',
  searchProfiles: '/api/v2/me/search-profiles',
  searchProfile: '/api/v2/me/search-profiles/{search_profile_id}',
  onboarding: '/api/v2/onboarding',
  onboardingComplete: '/api/v2/onboarding/complete',
} as const satisfies Record<string, keyof paths>

/**
 * Resolve the one V2 template that takes a path parameter.
 *
 * A saved search id is a UUID string, and it is the *only* thing that goes in a V2
 * path: the owner never does. `/me/...` is the authorization model — the account
 * comes from the session cookie, so there is no user id here for a caller to
 * change (docs/AUTHENTICATION.md §Authorization).
 */
export function searchProfile(searchProfileId: string): string {
  return V2_ENDPOINTS.searchProfile.replace('{search_profile_id}', searchProfileId)
}
