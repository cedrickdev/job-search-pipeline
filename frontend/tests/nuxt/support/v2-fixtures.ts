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
  CandidateClaim,
  CandidateDocument,
  CandidateDocumentList,
  CandidateEvidence,
  CandidateEvidenceList,
  CandidateProfile,
  CandidateProfileDraft,
  Company,
  CompanyDetail,
  CompanyDiscoveryRun,
  CompanyGeoItem,
  CompanyGeoResponse,
  CompanyList,
  DocumentArtifact,
  DocumentVersion,
  GeoLocation,
  LLMConnection,
  LLMConnectionHealth,
  LLMConnectionList,
  OnboardingState,
  OpportunityGeoItem,
  OpportunityGeoResponse,
  SearchProfile,
  SearchProfileDraft,
  SessionWindow,
  SignedIn,
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
