// The `/api/v2/me` and `/api/v2/onboarding` surface: three reads and five writes.
//
// One file rather than the useQueries/useMutations split beside it, because that
// split follows V1's hooks file and there is no V1 equivalent of any of this. What
// holds these together is the account: every one of them is scoped to the session's
// user server-side, and none of them takes an owner (docs/AUTHENTICATION.md
// §Authorization).
//
// Two things here are load-bearing rather than plumbing.
//
// **A missing profile is `null`, not an error.** `GET /me/profile` answers 404 with
// `candidate_profile_not_found` when onboarding has not saved one — deliberately, so
// that "no profile" and "a profile with empty fields" stay distinguishable — and the
// onboarding form is exactly the screen that needs to tell them apart. Mapping that
// one code to `null` is what keeps it out of the query's `error`, and it is the
// reason `ApiError.code` exists.
//
// **Every write invalidates `onboarding` too.** The counts behind `may_complete` are
// derived from the profile and the searches, so a save that refreshed only its own
// key would leave the "finish" button disabled after the step that enabled it.
import { computed, toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import { useSessionStore } from '~/stores/session'
import type {
  Account,
  CandidateProfile,
  CandidateProfileDraft,
  OnboardingState,
  SearchProfile,
  SearchProfileDraft,
  SearchProfileList,
} from '~/types/v2'
import { ApiError, apiDelete, apiGet, apiPost, apiPut } from '~/utils/api-client'
import { V2_ENDPOINTS, searchProfile as searchProfilePath } from '~/utils/endpoints'
import { invalidate, useApiQuery } from './useApiQuery'
import { useMutation } from './useMutation'

/** The keys every write below refreshes. Prefixes, expanded by `invalidate`. */
const ACCOUNT_KEYS = ['me', 'onboarding'] as const

/** This account's candidate profile, or `null` when it has never been saved. */
export function useProfileQuery() {
  return useApiQuery<CandidateProfile | null>('me:profile', async () => {
    try {
      return await apiGet<CandidateProfile>(V2_ENDPOINTS.profile)
    }
    catch (error) {
      if (error instanceof ApiError && error.code === 'candidate_profile_not_found') {
        return null
      }
      throw error
    }
  })
}

/**
 * This account's saved searches.
 *
 * `activeOnly` is part of the key, as the jobs filters are: the settings screen
 * shows paused searches and a discovery run wants only the live ones, and one
 * cache entry for both would serve whichever answer arrived last.
 */
export function useSearchProfilesQuery(activeOnly: MaybeRefOrGetter<boolean> = false) {
  const active = computed(() => toValue(activeOnly))
  return useApiQuery<SearchProfileList>(
    computed(() => `me:searches:${active.value ? 'active' : 'all'}`),
    () => apiGet<SearchProfileList>(
      `${V2_ENDPOINTS.searchProfiles}${active.value ? '?active_only=true' : ''}`),
  )
}

/** Which onboarding step to show, and whether finishing is allowed. */
export function useOnboardingQuery() {
  return useApiQuery<OnboardingState>('onboarding', () =>
    apiGet<OnboardingState>(V2_ENDPOINTS.onboarding))
}

/** Create or replace the candidate profile. `PUT`, because there is exactly one. */
export function useSaveProfile() {
  return useMutation<CandidateProfileDraft, CandidateProfile>(
    draft => apiPut<CandidateProfile>(V2_ENDPOINTS.profile, draft),
    { onSuccess: () => invalidate(...ACCOUNT_KEYS) },
  )
}

/**
 * The three writes on the saved-search collection.
 *
 * Grouped like `useJobActions`, and for the same reason: a screen that lists
 * searches needs all three, and one call site with three mutations keeps their
 * `isPending` flags separate — a delete in flight must not disable the create form.
 */
export function useSearchProfileActions() {
  const refresh = () => invalidate(...ACCOUNT_KEYS)
  return {
    create: useMutation<SearchProfileDraft, SearchProfile>(
      draft => apiPost<SearchProfile>(V2_ENDPOINTS.searchProfiles, draft),
      { onSuccess: refresh },
    ),
    update: useMutation<{ id: string, draft: SearchProfileDraft }, SearchProfile>(
      ({ id, draft }) => apiPut<SearchProfile>(searchProfilePath(id), draft),
      { onSuccess: refresh },
    ),
    // 204, so there is nothing to type but the absence of a body.
    remove: useMutation<string, null>(
      id => apiDelete<null>(searchProfilePath(id)),
      { onSuccess: refresh },
    ),
  }
}

/**
 * Finish onboarding, and put the returned account straight into the store.
 *
 * The stamp it sets is what every guard after this point branches on, so leaving
 * the store holding the pre-completion account would send the user back to the
 * onboarding page on their next navigation.
 */
export function useCompleteOnboarding() {
  const session = useSessionStore()
  return useMutation<void, Account>(
    () => apiPost<Account>(V2_ENDPOINTS.onboardingComplete),
    {
      onSuccess: async (account) => {
        session.setAccount(account)
        await invalidate(...ACCOUNT_KEYS)
      },
    },
  )
}
