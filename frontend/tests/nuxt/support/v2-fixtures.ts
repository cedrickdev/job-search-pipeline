// Fixture builders for the `/api/v2` payloads.
//
// Separate from fixtures.ts, and for the same reason app/types/v2.ts is separate
// from app/types/domain.ts: these shapes are generated from the backend's
// `response_model`s, so a field that changes there fails the typecheck here on the
// next `npm run gen:api`. V1's are hand-written and cannot make that promise.
//
// No real personal data — a project rule, not a habit. Every value below is
// invented, and the addresses use the reserved `.invalid` TLD so a test that leaks
// one into a request cannot reach anything.
import type {
  Account,
  Application,
  ApplicationEvent,
  ApplicationEventList,
  ApplicationList,
  ApplicationOutcome,
  ApplicationOutcomeList,
  CandidateClaim,
  CandidateDocument,
  CandidateDocumentList,
  CandidateEvidence,
  CandidateEvidenceList,
  CandidateProfile,
  CandidateProfileDraft,
  CareerAnalytics,
  CareerRecommendation,
  CareerRecommendationList,
  ChatActionExecution,
  ChatActionProposal,
  ChatActionProposalList,
  ChatMessage,
  ChatMessageList,
  Company,
  CompanyDetail,
  CompanyDiscoveryRun,
  CompanyGeoItem,
  CompanyGeoResponse,
  CompanyList,
  Conversation,
  ConversationList,
  DocumentArtifact,
  DocumentVersion,
  Entitlement,
  GeoLocation,
  InterviewAnswer,
  InterviewAnswerEvaluation,
  InterviewAnswerOutcome,
  InterviewPlan,
  InterviewQuestion,
  InterviewSession,
  InterviewSessionDetail,
  InterviewSessionList,
  InterviewSessionSummary,
  InterviewSessionSummaryList,
  InterviewTurn,
  LLMConnection,
  LLMConnectionHealth,
  LLMConnectionList,
  OnboardingState,
  OpportunityGeoItem,
  OpportunityGeoResponse,
  Plan,
  PlanList,
  RecommendationEvidence,
  SearchProfile,
  SearchProfileDraft,
  SessionReadiness,
  SessionWindow,
  SignedIn,
  StrategyChangeExecution,
  StrategyChangeProposal,
  StrategyChangeProposalDetail,
  StrategyChangeProposalList,
  SubscriptionOverview,
  UsageLine,
  UsagePeriod,
  UsageSnapshot,
} from '~/types/v2'

const USER_ID = '11111111-1111-4111-8111-111111111111'
const PROFILE_ID = '22222222-2222-4222-8222-222222222222'
const SEARCH_ID = '33333333-3333-4333-8333-333333333333'
const COMPANY_ID = '44444444-4444-4444-8444-444444444444'
const OPPORTUNITY_ID = '55555555-5555-4555-8555-555555555555'
const EVIDENCE_ID = '66666666-6666-4666-8666-666666666666'
const CLAIM_ID = '77777777-7777-4777-8777-777777777777'
const DOCUMENT_ID = '88888888-8888-4888-8888-888888888888'
const VERSION_ID = '99999999-9999-4999-8999-999999999999'
const CONNECTION_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const APPLICATION_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const CONVERSATION_ID = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const MESSAGE_ID = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
const PROPOSAL_ID = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee'
const EXECUTION_ID = 'ffffffff-ffff-4fff-8fff-ffffffffffff'
const SESSION_ID = '1a1a1a1a-1a1a-4a1a-8a1a-1a1a1a1a1a1a'
const QUESTION_ID = '2b2b2b2b-2b2b-4b2b-8b2b-2b2b2b2b2b2b'
const ANSWER_ID = '3c3c3c3c-3c3c-4c3c-8c3c-3c3c3c3c3c3c'
const INTERVIEW_EVALUATION_ID = '4d4d4d4d-4d4d-4d4d-8d4d-4d4d4d4d4d4d'
const SUMMARY_ID = '5e5e5e5e-5e5e-4e5e-8e5e-5e5e5e5e5e5e'
const OUTCOME_ID = '6f6f6f6f-6f6f-4f6f-8f6f-6f6f6f6f6f6f'
const RECOMMENDATION_ID = '7a7a7a7a-7a7a-4a7a-8a7a-7a7a7a7a7a7a'
const STRATEGY_PROPOSAL_ID = '8b8b8b8b-8b8b-4b8b-8b8b-8b8b8b8b8b8b'
const STRATEGY_EXECUTION_ID = '9c9c9c9c-9c9c-4c9c-8c9c-9c9c9c9c9c9c'

export function account(overrides: Partial<Account> = {}): Account {
  return {
    id: USER_ID,
    email: 'candidate@example.invalid',
    display_name: 'Test Candidate',
    status: 'ACTIVE',
    created_at: '2026-01-02T09:00:00Z',
    onboarding_completed_at: '2026-01-02T09:30:00Z',
    ...overrides,
  }
}

export function sessionWindow(overrides: Partial<SessionWindow> = {}): SessionWindow {
  return {
    issued_at: '2026-01-02T09:00:00Z',
    expires_at: '2026-01-09T09:00:00Z',
    last_seen_at: '2026-01-02T09:15:00Z',
    ...overrides,
  }
}

/** The reply to register, login and `GET /auth/session`. */
export function signedIn(overrides: Partial<SignedIn> = {}): SignedIn {
  return { account: account(), session: sessionWindow(), ...overrides }
}

export function profileDraft(overrides: Partial<CandidateProfileDraft> = {}): CandidateProfileDraft {
  return {
    display_name: 'Test Candidate',
    headline: null,
    base_location: null,
    languages: [],
    work_authorizations: [],
    availability: null,
    ...overrides,
  }
}

export function profile(overrides: Partial<CandidateProfile> = {}): CandidateProfile {
  return {
    id: PROFILE_ID,
    user_id: USER_ID,
    profile: profileDraft(),
    updated_at: '2026-01-02T09:20:00Z',
    ...overrides,
  }
}

export function searchDraft(overrides: Partial<SearchProfileDraft> = {}): SearchProfileDraft {
  return {
    name: 'Backend roles',
    is_active: true,
    areas: [{ kind: 'COUNTRY', country: 'CH', label: null }],
    queries: [],
    title_keywords: [],
    excluded_keywords: [],
    opportunity_types: [],
    contract_types: [],
    workplace_modes: [],
    posting_languages: [],
    source_keys: [],
    workload: null,
    ...overrides,
  }
}

export function search(overrides: Partial<SearchProfile> = {}): SearchProfile {
  return {
    id: SEARCH_ID,
    user_id: USER_ID,
    search: searchDraft(),
    created_at: '2026-01-02T09:25:00Z',
    updated_at: '2026-01-02T09:25:00Z',
    ...overrides,
  }
}

/**
 * An onboarding state, with the two derived flags derived.
 *
 * `is_complete` and `may_complete` follow from the counts on the backend
 * (backend/app/services/onboarding.py), so computing them here keeps a fixture from
 * describing a state the API cannot produce — "no profile, but finishing is allowed"
 * would test a screen that can never be reached. Either can still be overridden
 * explicitly, which is how the stale-state case gets written.
 */
export function onboarding(overrides: Partial<OnboardingState> = {}): OnboardingState {
  const merged = {
    has_profile: false,
    search_profiles: 0,
    active_search_profiles: 0,
    completed_at: null,
    ...overrides,
  }
  return {
    ...merged,
    is_complete: overrides.is_complete ?? merged.completed_at !== null,
    may_complete: overrides.may_complete
      ?? (merged.has_profile && merged.active_search_profiles > 0),
  }
}

/** The list wrapper `GET /me/search-profiles` answers with. */
export function searchList(...profiles: SearchProfile[]) {
  return { search_profiles: profiles }
}

/**
 * One employer, with nothing concluded about it.
 *
 * The default is the honest default the backend produces for a name seen on a
 * posting: `SEEDED`, no ATS, `UNKNOWN` spontaneous support. A test that wants a
 * detected platform or a decided channel says so, which keeps "we do not know" from
 * being something a fixture accidentally hides.
 */
export function company(overrides: Partial<Company> = {}): Company {
  return {
    id: COMPANY_ID,
    name: 'Logitech',
    normalized_name: 'logitech',
    website: 'https://www.logitech.invalid',
    careers_url: null,
    country: 'CH',
    identity_status: 'SEEDED',
    detected_ats: null,
    spontaneous_application: null,
    accepts_spontaneous_applications: null,
    locations: [],
    ...overrides,
  }
}

/** One page of employers. `total` defaults to what was passed, not to a guess. */
export function companyList(companies: Company[] = [company()],
                            overrides: Partial<CompanyList> = {}): CompanyList {
  return {
    companies,
    total: companies.length,
    limit: 20,
    offset: 0,
    ...overrides,
  }
}

export function companyDetail(overrides: Partial<CompanyDetail> = {}): CompanyDetail {
  return {
    company: company(),
    aliases: [],
    career_sites: [],
    discoveries: [],
    discovered_by: [],
    ...overrides,
  }
}

/** What a pass reports. Zeroes everywhere, so a test states its own numbers. */
export function discoveryRun(
  overrides: Partial<CompanyDiscoveryRun> = {}): CompanyDiscoveryRun {
  return {
    country: null,
    started_at: '2026-01-02T10:00:00Z',
    duration_ms: 12,
    providers: [],
    unusable_providers: [],
    is_complete: true,
    created: 0,
    matched: 0,
    ambiguous: 0,
    company_ids: [],
    health: [],
    warnings: [],
    links: { examined: 0, linked: 0, ambiguous: 0, unresolved: 0 },
    ...overrides,
  }
}

// --- Geo (Phase 7 reads, Phase 8 map) ------------------------------------------------
//
// A located row by default: an exact address with a point. The cases the map is strict
// about — a company fallback, a pure-remote role, an unresolved one — are spelled by
// overriding `status`/`location`, because those are exactly the shapes the projection
// (app/utils/map-projection.ts) must treat differently.

/** A geocoded location with a point. Pass `point: null` for an unplaceable one. */
export function geoLocation(overrides: Partial<GeoLocation> = {}): GeoLocation {
  return {
    point: { latitude: 46.5197, longitude: 6.6323 },
    precision: 'EXACT_ADDRESS',
    provenance: 'GEOCODED',
    confidence: 'HIGH',
    city: 'Lausanne',
    region: null,
    postal_code: null,
    country: 'CH',
    raw: null,
    geocoded_at: '2026-01-02T10:00:00Z',
    geocoder: 'nominatim',
    ...overrides,
  }
}

export function opportunityGeoItem(overrides: Partial<OpportunityGeoItem> = {}): OpportunityGeoItem {
  return {
    id: OPPORTUNITY_ID,
    title: 'Backend Engineer',
    company_name: 'Logitech',
    company_id: COMPANY_ID,
    application_url: 'https://boards.greenhouse.invalid/logitech/backend',
    contract_type: 'PERMANENT',
    opportunity_type: 'FULL_TIME',
    workplace_mode: 'ON_SITE',
    remote_scope: null,
    status: 'RESOLVED',
    location: geoLocation(),
    distance_meters: 1500,
    matched_radii: [],
    posted_at: '2026-01-01T08:00:00Z',
    discovered_at: '2026-01-02T09:00:00Z',
    posting_language: 'en',
    ...overrides,
  }
}

export function opportunityGeoResponse(
  opportunities: OpportunityGeoItem[] = [opportunityGeoItem()],
  overrides: Partial<OpportunityGeoResponse> = {}): OpportunityGeoResponse {
  return { opportunities, limit: 50, offset: 0, ...overrides }
}

export function companyGeoItem(overrides: Partial<CompanyGeoItem> = {}): CompanyGeoItem {
  return {
    company: company(),
    location: geoLocation({ precision: 'CITY' }),
    status: 'RESOLVED',
    is_headquarters: true,
    distance_meters: 3200,
    matched_radii: [],
    ...overrides,
  }
}

export function companyGeoResponse(
  companies: CompanyGeoItem[] = [companyGeoItem()],
  overrides: Partial<CompanyGeoResponse> = {}): CompanyGeoResponse {
  return { companies, limit: 50, offset: 0, ...overrides }
}

// --- Phase 10: candidate evidence and generated documents ----------------------------
//
// The default evidence is a CV summary — the one record the reference generator turns
// into a résumé summary line — and the default claim is the EXPERIENCE claim that cites
// it. The document fixtures build on those: a résumé whose one bullet quotes the same
// evidence, rendered to a one-page PDF. Every value is invented; no real personal data.

/** One attested fact. The default is a CV summary, the atom a summary line rests on. */
export function evidence(overrides: Partial<CandidateEvidence> = {}): CandidateEvidence {
  return {
    id: EVIDENCE_ID,
    kind: 'CV_SUMMARY',
    provenance: 'BASE_CV',
    summary: 'Backend engineer with 8 years of experience',
    reference_key: null,
    detail: null,
    issued_on: null,
    valid_until: null,
    source_document: null,
    recorded_at: '2026-01-02T09:40:00Z',
    ...overrides,
  }
}

/** One claim, citing evidence the profile already holds. `evidence_ids` is never empty. */
export function claim(overrides: Partial<CandidateClaim> = {}): CandidateClaim {
  return {
    id: CLAIM_ID,
    claim_type: 'EXPERIENCE',
    label: 'Senior Backend Engineer',
    detail: 'Acme, 2018–2026',
    evidence_ids: [EVIDENCE_ID],
    ...overrides,
  }
}

/** The whole attested record `GET /me/evidence` answers with. */
export function evidenceList(
  overrides: Partial<CandidateEvidenceList> = {}): CandidateEvidenceList {
  return { evidence: [evidence()], claims: [claim()], ...overrides }
}

/** Where a rendered PDF lives, as a download UI reads it. No bytes, no storage key. */
export function documentArtifact(
  overrides: Partial<DocumentArtifact> = {}): DocumentArtifact {
  return {
    media_type: 'application/pdf',
    byte_size: 48_000,
    page_count: 1,
    rendered_at: '2026-01-02T10:05:00Z',
    ...overrides,
  }
}

/**
 * One version. The default is a RENDERED résumé that cleared the guard, with its
 * artifact — the shape a download and a "latest usable" both need. A rejected or draft
 * attempt is written by overriding `status`, `guard_report` and `artifact: null`.
 */
export function documentVersion(overrides: Partial<DocumentVersion> = {}): DocumentVersion {
  return {
    id: VERSION_ID,
    version: 1,
    status: 'RENDERED',
    language: 'en',
    content: {
      kind: 'RESUME',
      full_name: 'Test Candidate',
      headline: 'Backend engineer',
      summary: {
        text: 'Backend engineer with 8 years of experience',
        evidence_ids: [EVIDENCE_ID],
      },
      experience: [{
        heading: 'Senior Backend Engineer',
        subheading: 'Acme, 2018–2026',
        evidence_ids: [EVIDENCE_ID],
        bullets: [{
          text: 'Rebuilt the checkout flow, cutting latency 30%',
          evidence_ids: [EVIDENCE_ID],
        }],
      }],
      education: [],
      skill_groups: [],
      languages: [],
    },
    guard_report: { ok: true, violations: [] },
    artifact: documentArtifact(),
    generator_key: 'deterministic-reference/1',
    created_at: '2026-01-02T10:05:00Z',
    ...overrides,
  }
}

/** A candidate's document for one posting, with its version history. */
export function candidateDocument(
  overrides: Partial<CandidateDocument> = {}): CandidateDocument {
  return {
    id: DOCUMENT_ID,
    candidate_profile_id: PROFILE_ID,
    opportunity_id: OPPORTUNITY_ID,
    document_type: 'RESUME',
    versions: [documentVersion()],
    latest_usable_version: 1,
    created_at: '2026-01-02T10:05:00Z',
    updated_at: '2026-01-02T10:05:00Z',
    ...overrides,
  }
}

/** This account's documents, most recently updated first, wrapped. */
export function documentList(
  documents: CandidateDocument[] = [candidateDocument()],
  overrides: Partial<CandidateDocumentList> = {}): CandidateDocumentList {
  return { documents, ...overrides }
}

// --- Phase 11: LLM connections -------------------------------------------------------
//
// The default is a local OpenAI-compatible connection: an endpoint, no credential, the
// one shape a keyless-and-enabled default exercises. The cases the settings UI is strict
// about — a CLI that stores no key, a hosted gateway with `has_api_key: true`, a disabled
// one — are written by overriding `provider_type`, `has_api_key` and `enabled`, because
// those are exactly the branches the form and the row treat differently. `has_api_key` is
// the only thing ever said about a credential; no fixture carries a key value, because no
// response ever does (§13).

/** One stored connection. The default is an enabled, keyless local endpoint. */
export function llmConnection(overrides: Partial<LLMConnection> = {}): LLMConnection {
  return {
    id: CONNECTION_ID,
    provider_type: 'LOCAL_OPENAI_COMPATIBLE',
    display_name: 'Local Ollama',
    base_url: 'http://127.0.0.1:11434/v1',
    model: 'llama3.2',
    has_api_key: false,
    custom_headers: {},
    enabled: true,
    is_default: true,
    priority: 100,
    created_at: '2026-01-02T09:00:00Z',
    updated_at: '2026-01-02T09:00:00Z',
    ...overrides,
  }
}

/** This account's connections, in priority-then-id order, wrapped. */
export function llmConnectionList(
  connections: LLMConnection[] = [llmConnection()],
  overrides: Partial<LLMConnectionList> = {}): LLMConnectionList {
  return { connections, ...overrides }
}

/** A health probe result. The default is a reachable provider with a latency. */
export function llmConnectionHealth(
  overrides: Partial<LLMConnectionHealth> = {}): LLMConnectionHealth {
  return {
    status: 'HEALTHY',
    detail: null,
    latency_ms: 42,
    ...overrides,
  }
}

export function application(overrides: Partial<Application> = {}): Application {
  return {
    id: APPLICATION_ID,
    state: 'PLANNED',
    channel: 'BROWSER',
    opportunity_id: OPPORTUNITY_ID,
    company_id: null,
    pinned_documents: [],
    attempt_count: 0,
    created_at: '2026-03-01T09:30:00Z',
    updated_at: '2026-03-01T09:30:00Z',
    ...overrides,
  }
}

export function applicationList(
  applications: Application[] = [application()],
): ApplicationList {
  return { applications }
}

export function applicationEvent(
  overrides: Partial<ApplicationEvent> = {},
): ApplicationEvent {
  return {
    event_type: 'CREATED',
    actor: 'SYSTEM',
    from_state: null,
    to_state: 'PLANNED',
    detail: 'opened for opportunity',
    reasons: [],
    occurred_at: '2026-03-01T09:30:00Z',
    ...overrides,
  }
}

export function applicationEventList(
  events: ApplicationEvent[] = [applicationEvent()],
): ApplicationEventList {
  return { events }
}

// --- Phase 13: career-chat control plane ---------------------------------------------
//
// The default thread has one exchange and one open proposal, and the proposal's default
// action is a NAVIGATE — the one action the page acts on itself (a confirmed NAVIGATE is
// the only client-side side effect). The cases the control plane is strict about — a
// terminal proposal (EXECUTED/REJECTED/FAILED/DISMISSED), a mutating action, a rejected
// confirm — are written by overriding `status`, `action` and the execution `outcome`,
// because those are exactly the branches the card and the store treat differently. No
// fixture carries a secret: `action` is the domain union verbatim, secret-free by
// construction, and an execution's `detail` is the server's own secret-free note.

/**
 * One chat thread's caption and activity — never its messages inline.
 *
 * The default thread is `GLOBAL` (spans the whole account, `scope_id` null) — the shape a
 * first visitor's thread has. An anchored thread is written by overriding `scope` and
 * `scope_id` together, because that pair is the domain invariant the badge reads: a
 * `GLOBAL` thread shows none, an anchored one names the resource it is bound to.
 */
export function conversation(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: CONVERSATION_ID,
    title: 'Backend roles in Lausanne',
    scope: 'GLOBAL',
    scope_id: null,
    is_archived: false,
    last_message_at: '2026-03-01T10:05:00Z',
    created_at: '2026-03-01T10:00:00Z',
    updated_at: '2026-03-01T10:05:00Z',
    ...overrides,
  }
}

/** This account's threads, most recent first, wrapped. */
export function conversationList(
  conversations: Conversation[] = [conversation()],
): ConversationList {
  return { conversations }
}

/** One stored turn. The default is a user turn; pass `role: 'ASSISTANT'` for a reply. */
export function chatMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: MESSAGE_ID,
    conversation_id: CONVERSATION_ID,
    role: 'USER',
    content: 'Which roles near Lausanne should I look at?',
    sequence: 1,
    llm_run_id: null,
    provider_key: null,
    created_at: '2026-03-01T10:00:00Z',
    ...overrides,
  }
}

/** One thread's transcript, oldest first, wrapped. */
export function chatMessageList(
  messages: ChatMessage[] = [chatMessage()],
): ChatMessageList {
  return { messages }
}

/**
 * One typed action the model proposed, awaiting a confirm or dismiss.
 *
 * The default action is a NAVIGATE to the opportunity map — inert, `PROPOSED` — because
 * that is the branch the page acts on itself once confirmed. Override `action` for a
 * mutating proposal and `status` for a terminal card.
 */
export function chatProposal(overrides: Partial<ChatActionProposal> = {}): ChatActionProposal {
  return {
    id: PROPOSAL_ID,
    conversation_id: CONVERSATION_ID,
    message_id: MESSAGE_ID,
    ordinal: 0,
    status: 'PROPOSED',
    summary: 'Open the opportunity map to see roles near you.',
    action: { kind: 'NAVIGATE', target: 'OPPORTUNITIES', opportunity_id: null },
    created_at: '2026-03-01T10:05:00Z',
    updated_at: '2026-03-01T10:05:00Z',
    ...overrides,
  }
}

/** One thread's proposals, oldest first, wrapped. */
export function chatProposalList(
  proposals: ChatActionProposal[] = [chatProposal()],
): ChatActionProposalList {
  return { proposals }
}

/**
 * The audited outcome of a confirmed proposal. The default SUCCEEDED; override `outcome`
 * and `detail` for a REJECTED (refused at the gate) or FAILED one.
 */
export function chatExecution(overrides: Partial<ChatActionExecution> = {}): ChatActionExecution {
  return {
    id: EXECUTION_ID,
    proposal_id: PROPOSAL_ID,
    outcome: 'SUCCEEDED',
    detail: null,
    result_ref: null,
    created_at: '2026-03-01T10:06:00Z',
    ...overrides,
  }
}

// --- Phase 14: interview simulator ---------------------------------------------------
//
// The default session is `CREATED` and open — the shape a just-opened panel has, before
// any question is asked. The states the panel is strict about — an `IN_PROGRESS` session
// with a pending question, a graded answer, a `COMPLETED` one with a summary — are written
// by overriding `status`/`is_active` and pairing the matching fixtures. The one rule the
// fixtures encode is the phase's: an evaluation carries only coaching (dimensions,
// strengths, improvements, a suggested answer) and never a forecast, and readiness is its
// own object — `overall`/`band` computed, `UNKNOWN` when nothing was evaluated. No forecast
// field exists to set. Every value is invented; no real personal data.

/** A coverage plan: what the session intends to exercise and how long it should run. */
export function interviewPlan(overrides: Partial<InterviewPlan> = {}): InterviewPlan {
  return {
    mode: 'BEHAVIORAL',
    target_question_count: 4,
    topics: [
      { label: 'Ownership', question_type: 'BEHAVIORAL', target_questions: 2 },
      { label: 'Collaboration', question_type: 'BEHAVIORAL', target_questions: 2 },
    ],
    ...overrides,
  }
}

/** One practice session. The default is `CREATED` and open — no question asked yet. */
export function interviewSession(overrides: Partial<InterviewSession> = {}): InterviewSession {
  return {
    id: SESSION_ID,
    candidate_profile_id: PROFILE_ID,
    opportunity_id: OPPORTUNITY_ID,
    application_id: null,
    title: 'Behavioral practice · Backend Engineer',
    mode: 'BEHAVIORAL',
    style: 'COACHING',
    difficulty: 'INTERMEDIATE',
    language: null,
    status: 'CREATED',
    is_active: true,
    plan: interviewPlan(),
    ended_at: null,
    created_at: '2026-03-02T09:00:00Z',
    updated_at: '2026-03-02T09:00:00Z',
    ...overrides,
  }
}

/** This account's sessions, newest first, wrapped. */
export function interviewSessionList(
  sessions: InterviewSession[] = [interviewSession()],
): InterviewSessionList {
  return { sessions }
}

/** One question. The default is the first, depth-0 question the plan discharges. */
export function interviewQuestion(overrides: Partial<InterviewQuestion> = {}): InterviewQuestion {
  return {
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
    ...overrides,
  }
}

/** What `next-question` returns: the session as it now stands and the question to answer. */
export function interviewTurn(overrides: Partial<InterviewTurn> = {}): InterviewTurn {
  return {
    session: interviewSession({ status: 'IN_PROGRESS' }),
    question: interviewQuestion(),
    ...overrides,
  }
}

/** One recorded answer. The default is a typed answer with no transcript confidence. */
export function interviewAnswer(overrides: Partial<InterviewAnswer> = {}): InterviewAnswer {
  return {
    id: ANSWER_ID,
    session_id: SESSION_ID,
    question_id: QUESTION_ID,
    format: 'TEXT',
    content: 'I noticed our deploys were flaky, so I owned the fix end to end.',
    transcript_confidence: null,
    answered_at: '2026-03-02T09:06:00Z',
    ...overrides,
  }
}

/**
 * One answer's coaching — dimensions, strengths, improvements, a suggested answer.
 *
 * No forecast: the shape has no probability, no verdict and no readiness, by construction.
 * A degraded submit returns no evaluation at all (`null`), which is written at the outcome.
 */
export function interviewEvaluation(
  overrides: Partial<InterviewAnswerEvaluation> = {}): InterviewAnswerEvaluation {
  return {
    id: INTERVIEW_EVALUATION_ID,
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
    ...overrides,
  }
}

/** What a submit returns: the moved session, the recorded answer, and its coaching. */
export function interviewOutcome(
  overrides: Partial<InterviewAnswerOutcome> = {}): InterviewAnswerOutcome {
  return {
    session: interviewSession({ status: 'IN_PROGRESS' }),
    answer: interviewAnswer(),
    evaluation: interviewEvaluation(),
    ...overrides,
  }
}

/**
 * A session's readiness — the platform's own coaching signal, not a forecast.
 *
 * The default is the honest empty state: nothing evaluated, so `overall` is null and `band`
 * is `UNKNOWN` (never a low score). A graded session is written by overriding `overall`,
 * `overall_percent`, `band` and the dimension means together.
 */
export function sessionReadiness(overrides: Partial<SessionReadiness> = {}): SessionReadiness {
  return {
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
    computed_at: '2026-03-02T09:00:00Z',
    ...overrides,
  }
}

/** A graded readiness — a session with one strong answer behind it, for the trend and summary. */
export function gradedReadiness(overrides: Partial<SessionReadiness> = {}): SessionReadiness {
  return sessionReadiness({
    overall: 0.62,
    overall_percent: 62,
    band: 'DEVELOPING',
    coverage: 0.25,
    answered_questions: 1,
    evaluated_answers: 1,
    dimensions: [
      { dimension: 'CLARITY', mean_score: 0.8, evaluated_count: 1, weight: 0.15 },
      { dimension: 'RELEVANCE', mean_score: null, evaluated_count: 0, weight: 0.2 },
      { dimension: 'COMPLETENESS', mean_score: null, evaluated_count: 0, weight: 0.2 },
      { dimension: 'STRUCTURE', mean_score: 0.6, evaluated_count: 1, weight: 0.25 },
      { dimension: 'SPECIFICITY', mean_score: null, evaluated_count: 0, weight: 0.2 },
    ],
    ...overrides,
  })
}

/** A finalized session's summary — its computed readiness paired with cleared coaching prose. */
export function interviewSummary(
  overrides: Partial<InterviewSessionSummary> = {}): InterviewSessionSummary {
  return {
    id: SUMMARY_ID,
    session_id: SESSION_ID,
    headline: 'A solid first pass — tighten your STAR structure.',
    strengths: ['Clear ownership stories.'],
    focus_areas: ['Quantify the outcomes you drove.'],
    questions_asked: 1,
    answers_evaluated: 1,
    generator_key: 'deterministic-interview/1',
    readiness: gradedReadiness(),
    created_at: '2026-03-02T09:30:00Z',
    ...overrides,
  }
}

/** The readiness history: completed sessions' summaries, newest first, wrapped. */
export function interviewSummaryList(
  summaries: InterviewSessionSummary[] = [interviewSummary()],
): InterviewSessionSummaryList {
  return { summaries }
}

/**
 * One session's full detail. The default is an `IN_PROGRESS` session with one question
 * asked and not yet answered — the state the store derives a current question from.
 */
export function interviewDetail(
  overrides: Partial<InterviewSessionDetail> = {}): InterviewSessionDetail {
  return {
    session: interviewSession({ status: 'IN_PROGRESS' }),
    questions: [interviewQuestion()],
    answers: [],
    evaluations: [],
    summary: null,
    ...overrides,
  }
}

// --- Phase 15: outcomes, analytics, recommendations and strategy proposals ------------
//
// The default outcome is an effective ACKNOWLEDGED milestone — the shape the timeline offers correct
// and retract on. A superseded or retracted row is written by overriding `status`/`is_effective`. The
// analytics default is a small but matured, non-empty funnel, so a test reads real rates rather than
// the empty state (written with `window.is_empty`). The recommendation cites one rate as its evidence
// and carries nothing it can execute; the strategy proposal defaults to a non-sensitive search edit,
// with `is_sensitive: true` written for the loosening case the approval gate treats specially. No real
// personal data — every value is invented.

/** One recorded milestone. The default is an effective ACKNOWLEDGED outcome, open to correction. */
export function applicationOutcome(
  overrides: Partial<ApplicationOutcome> = {}): ApplicationOutcome {
  return {
    id: OUTCOME_ID,
    application_id: APPLICATION_ID,
    kind: 'ACKNOWLEDGED',
    source: 'MANUAL_USER',
    status: 'EFFECTIVE',
    occurred_at: '2026-03-05T09:00:00Z',
    recorded_at: '2026-03-05T09:05:00Z',
    supersedes_id: null,
    detail: null,
    is_effective: true,
    is_terminal: false,
    is_correction: false,
    ...overrides,
  }
}

/** One application's outcomes, oldest first, wrapped. */
export function applicationOutcomeList(
  outcomes: ApplicationOutcome[] = [applicationOutcome()],
): ApplicationOutcomeList {
  return { outcomes }
}
/**
 * The funnel report. The default is a small, matured funnel — 10 submitted narrowing to 1 accepted —
 * so a test reads real rates and timings rather than the empty state. The empty state is written by
 * overriding `window`/`funnel` with `is_empty: true`.
 */
export function careerAnalytics(overrides: Partial<CareerAnalytics> = {}): CareerAnalytics {
  const window = {
    earliest_applied_at: '2026-02-01T09:00:00Z',
    latest_applied_at: '2026-03-01T09:00:00Z',
    is_empty: false,
  }
  return {
    analytics_version: 'career-analytics/1.0',
    window,
    funnel: {
      window,
      censoring: {
        as_of: '2026-03-20T09:00:00Z',
        observation_horizon_days: 30,
        mature_count: 10,
        censored_count: 2,
        total_count: 12,
      },
      stages: [
        { stage: 'SUBMITTED', applications: 10 },
        { stage: 'ACKNOWLEDGED', applications: 6 },
        { stage: 'SCREEN', applications: 4 },
        { stage: 'ASSESSMENT', applications: 3 },
        { stage: 'INTERVIEW', applications: 2 },
        { stage: 'OFFER', applications: 1 },
        { stage: 'ACCEPTED', applications: 1 },
      ],
    },
    rates: [
      { kind: 'RESPONSE', numerator: 6, denominator: 10, rate: 0.6, rate_percent: 60, sample_size: 10 },
      { kind: 'INTERVIEW_CONVERSION', numerator: 2, denominator: 10, rate: 0.2, rate_percent: 20, sample_size: 10 },
      { kind: 'OFFER_CONVERSION', numerator: 1, denominator: 10, rate: 0.1, rate_percent: 10, sample_size: 10 },
      { kind: 'ACCEPTANCE', numerator: 1, denominator: 1, rate: 1, rate_percent: 100, sample_size: 1 },
    ],
    timings: [
      { kind: 'TIME_TO_FIRST_RESPONSE', median_days: 4, p25_days: 2, p75_days: 7, sample_size: 6 },
      { kind: 'TIME_TO_INTERVIEW', median_days: 12, p25_days: 9, p75_days: 15, sample_size: 2 },
      { kind: 'TIME_TO_OFFER', median_days: 21, p25_days: 21, p75_days: 21, sample_size: 1 },
      { kind: 'TIME_TO_DECISION', median_days: 28, p25_days: 28, p75_days: 28, sample_size: 1 },
    ],
    breakdowns: [
      {
        dimension: 'ROLE_FAMILY',
        cells: [
          {
            dimension: 'ROLE_FAMILY',
            key: 'SOFTWARE_ENGINEERING',
            applications: 8,
            rates: [
              { kind: 'RESPONSE', numerator: 5, denominator: 8, rate: 0.625, rate_percent: 63, sample_size: 8 },
            ],
          },
        ],
      },
    ],
    computed_at: '2026-03-20T09:00:00Z',
    ...overrides,
  }
}
/** One cited metric. The default cites the response rate — the auditable number behind advice. */
export function recommendationEvidence(
  overrides: Partial<RecommendationEvidence> = {}): RecommendationEvidence {
  return {
    ordinal: 0,
    dimension: 'ROLE_FAMILY',
    dimension_key: 'SOFTWARE_ENGINEERING',
    rate_kind: 'RESPONSE',
    timing_kind: null,
    numerator: 5,
    denominator: 8,
    median_days: null,
    sample_size: 8,
    detail: 'Software engineering roles responded at 63% (5/8).',
    ...overrides,
  }
}

/** One evidence-backed suggestion — advice, never a lever. The default is HIGH confidence. */
export function careerRecommendation(
  overrides: Partial<CareerRecommendation> = {}): CareerRecommendation {
  return {
    id: RECOMMENDATION_ID,
    kind: 'PRIORITIZE_ROLE_FAMILY',
    analytics_version: 'career-analytics/1.0',
    summary: 'Lean into software engineering roles — they respond best.',
    detail: 'These roles answer at more than double the rate of your other applications.',
    evidence: [recommendationEvidence()],
    confidence: 'HIGH',
    min_evidence_sample_size: 8,
    generator_key: 'deterministic-recommendations/1',
    llm_run_id: null,
    created_at: '2026-03-20T09:10:00Z',
    ...overrides,
  }
}

/** This account's recommendations, most recent first, wrapped. */
export function careerRecommendationList(
  recommendations: CareerRecommendation[] = [careerRecommendation()],
): CareerRecommendationList {
  return { recommendations }
}
/**
 * One reviewable change. The default is a non-sensitive search edit — widening keywords — in the open
 * `PROPOSED` state the queue acts on. The loosening case the gate treats specially is written with
 * `is_sensitive: true` (e.g. a SET_MINIMUM_SCORE that lowers the floor); a terminal, settled card is
 * written by overriding `status`/`is_open`.
 */
export function strategyProposal(
  overrides: Partial<StrategyChangeProposal> = {}): StrategyChangeProposal {
  return {
    id: STRATEGY_PROPOSAL_ID,
    target: 'SEARCH_PROFILE',
    target_id: SEARCH_ID,
    change_kind: 'SET_SEARCH_KEYWORDS',
    summary: 'Add "platform engineer" to your backend search.',
    status: 'PROPOSED',
    is_open: true,
    is_sensitive: false,
    field_changes: [
      { field: 'title_keywords', before: 'backend', after: 'backend, platform engineer' },
    ],
    target_version: '2026-03-01T09:25:00Z',
    source_recommendation_id: RECOMMENDATION_ID,
    generator_key: 'deterministic-strategy/1',
    llm_run_id: null,
    created_at: '2026-03-20T09:15:00Z',
    updated_at: '2026-03-20T09:15:00Z',
    expires_at: '2026-03-27T09:15:00Z',
    ...overrides,
  }
}

/** A set of proposals — the pending queue or the full history, wrapped. */
export function strategyProposalList(
  proposals: StrategyChangeProposal[] = [strategyProposal()],
): StrategyChangeProposalList {
  return { proposals }
}

/** The record of one approval attempt. The default SUCCEEDED; override `outcome`/`detail` otherwise. */
export function strategyExecution(
  overrides: Partial<StrategyChangeExecution> = {}): StrategyChangeExecution {
  return {
    id: STRATEGY_EXECUTION_ID,
    proposal_id: STRATEGY_PROPOSAL_ID,
    outcome: 'SUCCEEDED',
    succeeded: true,
    observed_target_version: '2026-03-01T09:25:00Z',
    detail: null,
    result_ref: null,
    created_at: '2026-03-20T09:20:00Z',
    ...overrides,
  }
}

/** One proposal paired with its execution record, if it has been acted on. */
export function strategyProposalDetail(
  overrides: Partial<StrategyChangeProposalDetail> = {}): StrategyChangeProposalDetail {
  return {
    proposal: strategyProposal(),
    execution: null,
    ...overrides,
  }
}

// --- Phase 16: SaaS subscriptions & billing ---------------------------------------------
//
// The catalogue is server truth the client only renders (§17, §61), so these fixtures spell
// out the same three-tier shape the backend seeds — free, pro, scale — including the one
// unlimited meter that proves `is_unlimited` is a real granted value, not inferred from a null
// limit. No `external_price_id` field exists on the type, so a fixture cannot carry the provider
// handle a checkout needs server-side.

const PLAN_ID = 'a1a1a1a1-b2b2-4c3c-8d4d-e5e5e5e5e5e5'

/** One entitlement row: a capability, its measure and its ceiling. `limit` null means unlimited. */
export function entitlement(overrides: Partial<Entitlement> = {}): Entitlement {
  return {
    key: 'ACTIVE_SEARCH_PROFILES',
    measure: 'CONCURRENT',
    limit: 1,
    is_unlimited: false,
    ...overrides,
  }
}

/** One plan as the catalogue serves it. Defaults to the free tier; override for a paid one. */
export function plan(overrides: Partial<Plan> = {}): Plan {
  return {
    id: PLAN_ID,
    slug: 'free',
    name: 'Free',
    description: 'Get started at no cost.',
    is_free: true,
    is_active: true,
    is_public: true,
    price_amount_cents: null,
    currency: null,
    billing_interval: null,
    entitlements: [
      entitlement({ key: 'ACTIVE_SEARCH_PROFILES', measure: 'CONCURRENT', limit: 1 }),
      entitlement({ key: 'APPLICATION_SUBMISSIONS', measure: 'PER_PERIOD', limit: 5 }),
    ],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

/**
 * The public catalogue cheapest-first — free, pro, scale — mirroring the backend seed.
 *
 * `scale` leaves its token meter unlimited (a null limit with `is_unlimited` true), so a surface
 * that reads `is_unlimited` rather than inferring from the null is exercised.
 */
export function planList(overrides: Partial<PlanList> = {}): PlanList {
  return {
    plans: [
      plan(),
      plan({
        slug: 'pro',
        name: 'Pro',
        description: 'For an active search.',
        is_free: false,
        price_amount_cents: 1900,
        currency: 'usd',
        billing_interval: 'MONTHLY',
        entitlements: [
          entitlement({ key: 'ACTIVE_SEARCH_PROFILES', measure: 'CONCURRENT', limit: 5 }),
          entitlement({ key: 'APPLICATION_SUBMISSIONS', measure: 'PER_PERIOD', limit: 100 }),
        ],
      }),
      plan({
        slug: 'scale',
        name: 'Scale',
        description: 'For a high-volume search.',
        is_free: false,
        price_amount_cents: 4900,
        currency: 'usd',
        billing_interval: 'MONTHLY',
        entitlements: [
          entitlement({ key: 'ACTIVE_SEARCH_PROFILES', measure: 'CONCURRENT', limit: 25 }),
          entitlement({ key: 'LLM_TOKENS', measure: 'PER_PERIOD', limit: null, is_unlimited: true }),
        ],
      }),
    ],
    ...overrides,
  }
}

/** The billing window a snapshot sums against — its label and both ends. */
export function usagePeriod(overrides: Partial<UsagePeriod> = {}): UsagePeriod {
  return {
    label: '2026-04',
    start: '2026-04-01T00:00:00Z',
    end: '2026-05-01T00:00:00Z',
    ...overrides,
  }
}

/** This account's plan and live subscription. Defaults to the free tier with no subscription. */
export function subscriptionOverview(
  overrides: Partial<SubscriptionOverview> = {}): SubscriptionOverview {
  return {
    plan: plan(),
    is_paid: false,
    status: null,
    current_period_start: null,
    current_period_end: null,
    cancel_at_period_end: false,
    can_manage_billing: false,
    period: usagePeriod(),
    ...overrides,
  }
}

/** One metered capability's consumption: used, its ceiling and what is left. */
export function usageLine(overrides: Partial<UsageLine> = {}): UsageLine {
  return {
    key: 'APPLICATION_SUBMISSIONS',
    measure: 'PER_PERIOD',
    used: 2,
    limit: 5,
    remaining: 3,
    ...overrides,
  }
}

/** This account's usage against its plan — one line per metered capability. */
export function usageSnapshot(overrides: Partial<UsageSnapshot> = {}): UsageSnapshot {
  return {
    plan: plan(),
    is_paid: false,
    period: usagePeriod(),
    lines: [
      usageLine({ key: 'ACTIVE_SEARCH_PROFILES', measure: 'CONCURRENT', used: 1, limit: 1, remaining: 0 }),
      usageLine({ key: 'APPLICATION_SUBMISSIONS', measure: 'PER_PERIOD', used: 2, limit: 5, remaining: 3 }),
    ],
    ...overrides,
  }
}


