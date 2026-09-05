// Who is signed in — the one piece of backend data this app keeps in Pinia.
//
// stores/ui.ts states the rule this breaks ("backend data is not copied in here"),
// so the exception needs a reason rather than a preference. It is the router. A
// route middleware runs before any component exists, so it cannot call
// `useAsyncData`, and it has to answer "is there a session?" *before* deciding
// whether to render the page at all. Something outside the component tree has to
// hold that answer, and the alternative — every guard doing its own
// `GET /auth/session` — would ask the server once per navigation and still have no
// shared answer for the shell to render.
//
// The rule is kept where it matters: this store holds the account and the session
// window, and nothing else. The candidate profile, the saved searches and the
// onboarding counts are server state with pages of their own, and they stay in
// `useApiQuery`'s cache (composables/useAccount.ts). Copying them here is what
// would create a second source of truth.
//
// **`status` has three values, not two.** "We have not asked yet" is a different
// state from "we asked and there is nobody", and collapsing them is what produces
// a login form flashing on screen for a signed-in user on every page load. The
// middleware waits for `ensure()`; the shell renders from `status` and shows
// nothing account-shaped while it is `unknown`.
//
// **Nothing here is a credential.** The session token is an `HttpOnly` cookie this
// code cannot read, and the CSRF token is read from the cookie jar per request by
// `utils/api-client.ts`. Neither is stored, and there is no field for either
// (docs/AUTHENTICATION.md §The two cookies).
import { defineStore } from 'pinia'
import { clearCached } from '~/composables/useApiQuery'
import type { Account, SessionWindow, SignedIn } from '~/types/v2'
import { ApiError, apiGet, apiPost } from '~/utils/api-client'
import { V2_ENDPOINTS } from '~/utils/endpoints'

/** Credentials as the two `/auth` routes take them. */
export interface Credentials {
  email: string
  password: string
}

export interface Registration extends Credentials {
  display_name?: string | null
}

/**
 * The `useApiQuery` prefixes that belong to one account.
 *
 * Dropped on every session change, in both directions. Signing out must not leave
 * the profile in the cache for whoever sits down next, and signing *in* must not
 * inherit the previous account's — the requirement is cross-user isolation, and a
 * client-side cache is a place it can be lost after the API got it right.
 */
const ACCOUNT_CACHE_KEYS = ['me', 'onboarding'] as const

interface SessionState {
  account: Account | null
  session: SessionWindow | null
  status: 'unknown' | 'authenticated' | 'anonymous'
  /** The reason the last `GET /auth/session` failed for something other than 401. */
  error: unknown
  /** The in-flight hydration, so concurrent callers share one request. */
  inflight: Promise<boolean> | null
}

export const useSessionStore = defineStore('session', {
  state: (): SessionState => ({
    account: null,
    session: null,
    status: 'unknown',
    error: null,
    inflight: null,
  }),

  getters: {
    isAuthenticated: state => state.status === 'authenticated',
    /** Signed in, but `POST /onboarding/complete` has never stamped the account. */
    needsOnboarding: state =>
      state.account !== null && state.account.onboarding_completed_at === null,
    /** What to call the user in the shell. The address is the fallback, not a label. */
    label: state => state.account?.display_name ?? state.account?.email ?? null,
  },

  actions: {
    /**
     * Resolve the session once, and answer every later caller from memory.
     *
     * Returns whether there is a session, which is what a guard needs. Concurrent
     * callers (the middleware and the shell, on a first load) share one request;
     * without that the answer would be fetched twice on every cold navigation.
     */
    async ensure(): Promise<boolean> {
      if (this.status === 'authenticated') return true
      if (this.status === 'anonymous') return false
      if (this.inflight !== null) return this.inflight
      const request = this.load()
      this.inflight = request
      try {
        return await request
      }
      finally {
        this.inflight = null
      }
    },

    /** Ask the server again, whatever we already believe. */
    async refresh(): Promise<boolean> {
      this.status = 'unknown'
      return this.ensure()
    },

    /**
     * `GET /auth/session`, with 401 as an ordinary answer.
     *
     * A visitor with no cookie is not an error to report; it is the expected reply
     * and it means `anonymous`. Anything else — a 503 from an unreachable database,
     * a dropped connection — leaves `status` at `unknown` and records the cause, so
     * the next navigation asks again instead of telling the user they are signed
     * out on the strength of a network failure.
     */
    async load(): Promise<boolean> {
      try {
        this.apply(await apiGet<SignedIn>(V2_ENDPOINTS.session))
        return true
      }
      catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          this.forget()
          return false
        }
        this.error = error
        return false
      }
    },

    async signIn(credentials: Credentials): Promise<Account> {
      const signedIn = await apiPost<SignedIn>(V2_ENDPOINTS.login, credentials)
      this.adopt(signedIn)
      return signedIn.account
    },

    async signUp(registration: Registration): Promise<Account> {
      const signedIn = await apiPost<SignedIn>(V2_ENDPOINTS.register, registration)
      this.adopt(signedIn)
      return signedIn.account
    },

    /**
     * Revoke the session server-side, then forget it here.
     *
     * Deliberately cannot fail. The server call is what makes a logout real — it
     * revokes the row, so a copied cookie stops working — but if it does not
     * answer, the useful thing is still to end the session in this browser rather
     * than to leave the user looking at an error beside a button that no longer
     * does anything. A 401 is the same case one step further along: the session had
     * already expired, and there was nothing left to revoke.
     */
    async signOut(): Promise<void> {
      try {
        await apiPost(V2_ENDPOINTS.logout)
      }
      catch {
        // Intentionally swallowed; see above.
      }
      finally {
        this.forget()
      }
    },

    /**
     * Replace the account, for a write that changed it.
     *
     * `POST /onboarding/complete` answers with the account precisely because the
     * client's copy has just gone stale: `onboarding_completed_at` is the field the
     * guard below branches on, and re-fetching the session to learn a value the
     * response already carried would be a second round trip for nothing.
     */
    setAccount(account: Account): void {
      this.account = account
      this.status = 'authenticated'
    },

    /** Record a fresh session and drop whatever the last one had cached. */
    adopt(signedIn: SignedIn): void {
      clearCached(...ACCOUNT_CACHE_KEYS)
      this.apply(signedIn)
    },

    apply(signedIn: SignedIn): void {
      this.account = signedIn.account
      this.session = signedIn.session
      this.status = 'authenticated'
      this.error = null
    },

    forget(): void {
      this.account = null
      this.session = null
      this.status = 'anonymous'
      this.error = null
      clearCached(...ACCOUNT_CACHE_KEYS)
    },
  },
})
