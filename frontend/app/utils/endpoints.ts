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
