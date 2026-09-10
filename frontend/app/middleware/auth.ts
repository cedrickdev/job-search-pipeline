// The route guard for the pages that need an account. Named, not global.
//
// `app/middleware/auth.ts` without `.global` means a page opts in with
// `definePageMeta({ middleware: 'auth' })`, and that is the whole design decision:
// the V1 pages — overview, jobs, analytics, settings — are **not** guarded, because
// V1's `/api/**` has no accounts. Its 29 operations answer without a session cookie
// (docs/AUTHENTICATION.md), so a guard in front of those screens would demand a
// login the data behind them does not know about. `/profile` and `/onboarding`, which
// read `/api/v2/me`, carry this — and so do the Phase 6 company screens, whose data
// is shared rather than owned but whose endpoints still answer 401 without a session.
//
// A global middleware with an exception list would invert that: every page added
// from here on would be guarded by default and the list would be the thing to
// remember. Opting in per page keeps "this screen needs an account" written on the
// screen that needs one.
//
// The guard is not the protection. The API refuses an unauthenticated request on its
// own — that is what `tests/test_v2_api_surface.py` pins for all thirteen protected
// operations — and this only decides which screen to show. A user who edits the
// route table in their own browser gets a page that 401s, not somebody's data.
import { useSessionStore } from '~/stores/session'

export default defineNuxtRouteMiddleware(async (to) => {
  const session = useSessionStore()

  // Resolved once per page load, not per navigation: `ensure()` answers from the
  // store as soon as the status is known.
  if (!(await session.ensure())) {
    // `redirect` carries the destination so the login form can return the user to
    // it. `fullPath`, so a query string survives; a relative path only, because a
    // guard that forwarded an absolute URL would be an open redirect.
    return navigateTo({ path: '/login', query: { redirect: to.fullPath } })
  }

  // An account that has never completed onboarding has no candidate profile and no
  // saved search, so every screen after this one would render empty. `/onboarding`
  // itself is exempt, or this would be a redirect loop.
  if (session.needsOnboarding && to.path !== '/onboarding') {
    return navigateTo('/onboarding')
  }
})
