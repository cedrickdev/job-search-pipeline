// A small in-memory `/api/v2/me` + `/api/v2/onboarding` server for the page tests.
//
// The onboarding and profile pages are both driven by state the *server* recomputes
// after every write: the step shown comes from `GET /onboarding`, not from anything
// the page remembers, and `POST /onboarding/complete` re-reads both facts and refuses
// with 409 if they are missing. A route table of fixed responses cannot express that
// — the interesting cases are precisely the ones where a write changes the next read
// — so these routes read and mutate one object instead.
//
// It is a fake, not a mock: it holds the two aggregates, derives the onboarding
// counts from them the way backend/app/services/onboarding.py does, and refuses what
// the API refuses. What it deliberately does *not* model is authorization — every
// route here answers as the signed-in account, because scoping is the backend's job
// and `tests/test_v2_api_surface.py` is where it is pinned.
import { account, onboarding, profile, search } from './v2-fixtures'
import type { Route } from './http'
import type {
  Account,
  CandidateProfile,
  CandidateProfileDraft,
  OnboardingState,
  SearchProfile,
  SearchProfileDraft,
} from '~/types/v2'

const SECOND_SEARCH_ID = '44444444-4444-4444-8444-444444444444'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function refusal(status: number, code: string, detail: string): Response {
  return json({ error: code, detail }, status)
}

function bodyOf<T>(init?: RequestInit): T {
  return JSON.parse(String(init?.body ?? '{}')) as T
}

/** The id at the end of `/me/search-profiles/{id}`. */
function idIn(url: string): string {
  return url.split('/').pop() ?? ''
}

export interface AccountApi {
  /** Pass to `stubFetch`. Ordered: the specific paths come before the general ones. */
  routes: Route[]
  profile: CandidateProfile | null
  searches: SearchProfile[]
  completedAt: string | null
  /** What `GET /api/v2/onboarding` would answer right now. */
  onboardingState: () => OnboardingState
  account: () => Account
}

/**
 * Build the fake. Everything starts empty — a freshly registered account — and each
 * field can be set before mounting to describe any other state.
 */
export function accountApi(): AccountApi {
  const api: AccountApi = {
    routes: [],
    profile: null,
    searches: [],
    completedAt: null,
    onboardingState: () => onboarding({
      has_profile: api.profile !== null,
      search_profiles: api.searches.length,
      active_search_profiles: api.searches.filter(entry => entry.search.is_active).length,
      completed_at: api.completedAt,
    }),
    account: () => account({ onboarding_completed_at: api.completedAt }),
  }

  api.routes = [
    {
      match: '/api/v2/me/profile',
      method: 'PUT',
      respond: (_url, init) => {
        api.profile = profile({ profile: bodyOf<CandidateProfileDraft>(init) })
        return json(api.profile)
      },
    },
    {
      match: '/api/v2/me/profile',
      method: 'GET',
      respond: () => (api.profile === null
        // The API's answer for an account that has not written one yet; the query
        // turns this particular 404 into `null` rather than an error.
        ? refusal(404, 'candidate_profile_not_found', 'no profile')
        : json(api.profile)),
    },
    {
      match: '/api/v2/me/search-profiles/',
      method: 'PUT',
      respond: (url, init) => {
        const id = idIn(url)
        const index = api.searches.findIndex(entry => entry.id === id)
        if (index === -1) return refusal(404, 'search_profile_not_found', 'no such search')
        const updated = search({
          ...api.searches[index]!,
          search: bodyOf<SearchProfileDraft>(init),
          updated_at: '2026-01-03T10:00:00Z',
        })
        api.searches.splice(index, 1, updated)
        return json(updated)
      },
    },
    {
      match: '/api/v2/me/search-profiles/',
      method: 'DELETE',
      respond: (url) => {
        const id = idIn(url)
        if (!api.searches.some(entry => entry.id === id)) {
          return refusal(404, 'search_profile_not_found', 'no such search')
        }
        api.searches = api.searches.filter(entry => entry.id !== id)
        return new Response(null, { status: 204 })
      },
    },
    {
      match: '/api/v2/me/search-profiles',
      method: 'POST',
      respond: (_url, init) => {
        const first = search({ search: bodyOf<SearchProfileDraft>(init) })
        // A second search needs its own id, or a delete would hit both.
        const created = api.searches.length === 0 ? first : { ...first, id: SECOND_SEARCH_ID }
        api.searches = [...api.searches, created]
        return json(created, 201)
      },
    },
    {
      match: '/api/v2/me/search-profiles',
      method: 'GET',
      respond: (url) => {
        const activeOnly = url.includes('active_only=true')
        const listed = activeOnly
          ? api.searches.filter(entry => entry.search.is_active)
          : api.searches
        return json({ search_profiles: listed })
      },
    },
    // Before `/api/v2/onboarding`, which is a prefix of this path.
    {
      match: '/api/v2/onboarding/complete',
      method: 'POST',
      respond: () => {
        if (!api.onboardingState().may_complete) {
          return refusal(409, 'onboarding_incomplete', 'not ready')
        }
        api.completedAt = '2026-01-03T10:30:00Z'
        return json(api.account())
      },
    },
    { match: '/api/v2/onboarding', method: 'GET', respond: () => json(api.onboardingState()) },
    {
      match: '/api/v2/auth/session',
      method: 'GET',
      respond: () => json({
        account: api.account(),
        session: {
          issued_at: '2026-01-02T09:00:00Z',
          expires_at: '2026-01-09T09:00:00Z',
          last_seen_at: '2026-01-02T09:15:00Z',
        },
      }),
    },
    {
      match: '/api/v2/auth/logout',
      method: 'POST',
      respond: () => new Response(null, { status: 204 }),
    },
  ]

  return api
}
