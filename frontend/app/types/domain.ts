// Response shapes for the FastAPI surface, ported from webapp/src/api/types.ts.
//
// These are hand-written on purpose, and the reason is worth recording because
// it looks like the duplication the migration was told to remove.
//
// `openapi.json` is generated from the application itself (scripts/dump_openapi.py)
// and `api.d.ts` from that, so every path, method, path parameter, query
// parameter and *request* body below is machine-checked against FastAPI. But the
// V1 routes declare no `response_model`: FastAPI therefore documents each 200 as
// an empty schema, and `api.d.ts` types every response as `unknown`. Generating
// response types is impossible until the backend annotates its returns, so the
// choice is between typed-by-hand and untyped, not between generated and
// duplicated.
//
// The consequence is bounded, and deliberately so: a rename or removal on the
// backend fails `nuxt typecheck` through app/utils/endpoints.ts, which is the
// drift that actually breaks the app. A changed *field* inside a response is not
// caught by the compiler; the tests are what pin those.
export type Maybe<T> = T | null;

export type LlmBackend = "claude_cli" | "ollama" | "lmstudio";

export interface Settings {
  auto_apply: boolean;
  auto_apply_min_score: number;
  auto_apply_daily_cap: number;
  tailor_creativity: "conservative" | "balanced" | "bold";
  llm_backend: LlmBackend;
  llm_base_url: string;
  llm_model: string;
  schedule_enabled: boolean;
  schedule_time: string;
  schedule_cadence: "daily" | "weekdays";
}

export interface SettingsResponse {
  settings: Settings;
  status: "ok" | "missing" | "invalid";
}

// GET /api/runs/status — RunManager.status() payload. `state` flips to
// "running" while a discovery or full run is in flight; `last_run` carries the
// previous run's outcome once idle again.
export interface RunStatus {
  state: "idle" | "running";
  kind: "discovery" | "full" | null;
  started_at: string | null;
  last_run: {
    kind: "discovery" | "full";
    started_at: string;
    finished_at: string;
    ok: boolean;
    summary: string;
  } | null;
}

export interface JobCard {
  application_id: number;
  job_id: number;
  company: string;
  title: string;
  track: string;
  status: string;
  url: string;
  language: string;
  score: Maybe<number>;
  phone_screen_pct: Maybe<number>;
  red_flags?: Maybe<string>;
}

export interface Kpi<T> {
  value: T;
  target?: number;
  goal?: number;
}

export interface Overview {
  kpis: {
    phone_screen_readiness: Kpi<Maybe<number>>;
    response_rate: Maybe<number>;
    velocity: Kpi<number>;
    in_flight: number;
    replies_to_action: number;
  };
  today: JobCard[];
  borderline: JobCard[];
  auto_approved_today: JobCard[];
  followups_due: FollowupItem[];
  replies_to_action: JobCard[];
  upcoming_interviews: InterviewItem[];
  funnel: { stage: string; count: number; pct: Maybe<number> }[];
  status_breakdown: { status: string; count: number }[];
  source_health: { source: string; [k: string]: unknown }[];
}

export interface DayPoint {
  date: string;
  count: number;
}

export interface PhoneScreenPoint {
  date: string;
  value: number;
  n: number;
}

// Trends view (GET /api/analytics). Series are built only from reliably
// timestamped data (submitted_at, status_change events, cv_versions.created_at);
// no ratios are reconstructed from incomplete event history.
export interface Analytics {
  days: number;
  kpis: {
    phone_screen_readiness: Kpi<Maybe<number>>;
    response_rate: Maybe<number>;
    velocity: { value: number; goal: number; window_days: number };
  };
  funnel: { stage: string; count: number; pct: Maybe<number> }[];
  status_breakdown: { status: string; count: number }[];
  applications_per_day: DayPoint[];
  replies_per_day: DayPoint[];
  phone_screen_trend: { target: number; points: PhoneScreenPoint[] };
}

export interface JobsResponse {
  items?: JobCard[];
  board?: Record<string, JobCard[]>;
}

export interface FitReport {
  available: boolean;
  coverage_score?: number;
  risk_tier?: "GREEN" | "YELLOW" | "RED";
  matched_keywords?: string[];
  missing_keywords?: string[];
}

export interface CvVersion {
  id: number;
  language: string;
  phone_screen_pct: Maybe<number>;
  pdf_url: string;
  created_at: string;
}

export interface JobDetail {
  job: {
    id: number;
    company: string;
    title: string;
    url: string;
    location: Maybe<string>;
    language: string;
    description: Maybe<string>;
  };
  application: Maybe<{
    id: number;
    status: string;
    submitted_at: Maybe<string>;
    recruiter_email: Maybe<string>;
  }>;
  score: Maybe<{ score: number; reasoning: string }>;
  fit: FitReport;
  cv_versions: { en: Maybe<CvVersion>; fr: Maybe<CvVersion> };
  cover_letter: { en: Maybe<string>; fr: Maybe<string> };
  events: EventItem[];
  // A queued CV regeneration awaiting the next agentic run, or null. Tailoring
  // runs keyless in that background run, so the webapp can only show the queued
  // state; this clears (-> null) once the run renders the new CV.
  pending_regen: Maybe<{ request_id: number; notes: string; creativity: string; created_at: string }>;
  // The most recent regeneration of ANY status (or null). Unlike pending_regen
  // (which clears once resolved), this persists so the drawer can confirm a
  // completed regen and surface a failed one with its reason.
  last_regen: Maybe<{
    request_id: number;
    status: "pending" | "done" | "failed";
    notes: string;
    creativity: string;
    created_at: string;
    resolved_at: Maybe<string>;
    detail: Maybe<string>;
  }>;
  // The job's most recent background apply request of ANY status (or null).
  // Drives the drawer Apply-now lifecycle banner. Non-terminal (pending/
  // in_progress) means a worker is mid-apply; the drawer polls until it settles.
  last_apply: Maybe<{
    request_id: number;
    status: "pending" | "in_progress" | "applied" | "needs_you" | "failed";
    channel: Maybe<string>;
    detail: Maybe<string>;
    screenshot_path: Maybe<string>;
    created_at: string;
    resolved_at: Maybe<string>;
  }>;
}

// Timeline rows for the drawer Activity tab. Matches the dicts returned by
// server/queries.py:job_events (events keyed to the job OR its application).
export interface EventItem {
  id: number;
  event_type: string;
  detail: Maybe<string>;
  source: string;
  created_at: string;
}

// Matches the dicts returned by server/followups.py (applied_no_reply /
// recruiter_no_outbound).
export interface FollowupItem {
  kind: "applied_no_reply" | "recruiter_no_outbound";
  application_id: number;
  job_id: number;
  company: string;
  title: string;
  days: number;
  since: string;
}

// POST /api/jobs/{id}/draft_followup — deterministic, mandate-gated template
// draft. mandate_ok=false + flags means it did not clear the gate.
export interface FollowupDraft {
  subject: string;
  body: string;
  language: string;
  mandate_ok: boolean;
  flags: string[];
}

export interface Interview {
  id: number;
  round_label: string;
  scheduled_for: Maybe<string>;
  outcome: Maybe<string>;
  notes: Maybe<string>;
  created_at: string;
}

export interface PrepData {
  notes_md: string;
  likely_questions: Maybe<string[]>;
  company_research: Maybe<string[]>;
  talking_points: Maybe<string[]>;
  generated_at: Maybe<string>;
  interviews: Interview[];
  // Present only on a fresh /prep/generate response (fail-closed): a draft that
  // did not clear the mandate gate has mandate_ok=false + flags and
  // generated_at=null — shown with a warning, never persisted as ready.
  mandate_ok?: boolean;
  flags?: string[];
}

// Shape matches `prep_store.upcoming_interviews` exactly: `job_id` + `title`
// drive drawer navigation (`open(job_id)`), not `application_id` (which the
// frontend never needs — interview PATCH is keyed by the interview `id`).
export interface InterviewItem {
  id: number;
  job_id: number;
  company: string;
  title: string;
  round_label: string;
  scheduled_for: Maybe<string>;
}
