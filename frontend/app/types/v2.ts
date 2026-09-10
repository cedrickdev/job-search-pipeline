// The `/api/v2` payload types — derived, not hand-written.
//
// This file is the counterpoint to types/domain.ts, and the difference is the
// backend's, not a change of taste. The V1 routes declare no `response_model`, so
// FastAPI documents each 200 as an empty schema and openapi-typescript can only
// type them `unknown` — which is why domain.ts spells V1's responses out by hand.
// Every V2 route declares one (backend/app/api/schemas.py), so the generated
// document already carries the exact field list and these are aliases into it.
//
// The practical consequence: a field renamed or dropped on the backend fails
// `nuxt typecheck` here after `npm run gen:api`, and `npm run check:api` fails in
// CI if the committed document has drifted from the application. Nothing below is
// a second source of truth, which is the whole reason to prefer aliases over a
// second set of interfaces.
//
// One thing is deliberately *not* here: a type for the session or CSRF token.
// Neither ever appears in a response body — they arrive as `Set-Cookie` headers,
// and the session cookie is `HttpOnly` and unreadable by this app on purpose
// (docs/AUTHENTICATION.md §The two cookies). There is nothing to model.
import type { components } from '~/types/api'

type Schemas = components['schemas']

/** The whole of what a client learns about its own account. No credentials. */
export type Account = Schemas['AccountResponse']

/** The current session's window: issued, expires, last seen. No id, no digest. */
export type SessionWindow = Schemas['SessionResponse']

/** The reply to register, login and `GET /auth/session`. */
export type SignedIn = Schemas['SignedInResponse']

/** A candidate profile as submitted: no id, no owner, no timestamps. */
export type CandidateProfileDraft = Schemas['CandidateProfileDraft']

/** A saved profile, echoed back with its id and `updated_at`. */
export type CandidateProfile = Schemas['CandidateProfileResponse']

/** A saved search as submitted. `areas` needs at least one entry. */
export type SearchProfileDraft = Schemas['SearchProfileDraft']

/** A saved search, echoed back with its id and timestamps. */
export type SearchProfile = Schemas['SearchProfileResponse']

/** The list wrapper — every V2 response is an object, never a bare array. */
export type SearchProfileList = Schemas['SearchProfileListResponse']

/** Which onboarding step to show, and whether finishing is allowed. */
export type OnboardingState = Schemas['OnboardingStateResponse']

/** One employer, as the directory describes it. Shared: no owner, no `user_id`. */
export type Company = Schemas['CompanyResponse']

/** One page of employers, with the window it came from. */
export type CompanyList = Schemas['CompanyListResponse']

/** One employer with its aliases, its careers endpoints and its provenance. */
export type CompanyDetail = Schemas['CompanyDetailResponse']

/** What a discovery pass did: what ran, what was written, what stayed ambiguous. */
export type CompanyDiscoveryRun = Schemas['CompanyDiscoveryRunResponse']

/** Which ATS an employer publishes on. A closed set — Phase 6 supports three. */
export type AtsPlatform = Schemas['AtsPlatform']

/** `SUPPORTED | NOT_SUPPORTED | UNKNOWN`. The third is not a missing answer. */
export type SpontaneousSupport = Schemas['SpontaneousApplicationSupport']

export type Location = Schemas['Location']
export type LanguageProficiency = Schemas['LanguageProficiency']
export type LanguageLevel = LanguageProficiency['level']

/** One geographic area of a saved search: a country, a radius, or remote-only. */
export type SearchArea = SearchProfileDraft['areas'][number]

/**
 * Every `error` slug `/api/v2` can answer with, as one union.
 *
 * Written out rather than derived: FastAPI documents the *shape* of an error body
 * but not the set of slugs, so this is the one V2 contract the generator cannot
 * check. It is a closed set on the backend — `backend/app/api/errors.py` maps each
 * exception to exactly one of these — and having it typed here is what makes a
 * `switch` on `ApiError.code` exhaustive in the compiler's eyes.
 */
export type V2ErrorCode =
  | 'account_disabled'
  | 'account_locked'
  | 'candidate_profile_not_found'
  | 'company_not_found'
  | 'conflict'
  | 'csrf_failed'
  | 'database_unavailable'
  | 'email_already_registered'
  | 'invalid_credentials'
  | 'not_authenticated'
  | 'onboarding_incomplete'
  | 'search_profile_not_found'
  | 'validation_failed'
