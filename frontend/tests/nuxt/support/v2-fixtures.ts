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
  CandidateProfile,
  CandidateProfileDraft,
  Company,
  CompanyDetail,
  CompanyDiscoveryRun,
  CompanyList,
  OnboardingState,
  SearchProfile,
  SearchProfileDraft,
  SessionWindow,
  SignedIn,
} from '~/types/v2'

const USER_ID = '11111111-1111-4111-8111-111111111111'
const PROFILE_ID = '22222222-2222-4222-8222-222222222222'
const SEARCH_ID = '33333333-3333-4333-8333-333333333333'
const COMPANY_ID = '44444444-4444-4444-8444-444444444444'

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
