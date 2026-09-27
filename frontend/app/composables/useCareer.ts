// The `/api/v2/career` surface: the "measure" and "recommend" links of the spine, read-only
// but for the one write that adds a fresh set of suggestions.
//
// Two rules of the loop show up here as plumbing decisions.
//
// **Analytics is a pure read, and generation never touches it.** The report counts the funnel,
// the four rates, the timings and the breakdowns; nothing in this file can write execution state
// (docs/CAREER_INTELLIGENCE.md §measure). Generating recommendations recomputes that report
// server-side and *adds* observations to a write-once store — it edits no prior suggestion — so
// the mutation invalidates only the recommendations key, never the analytics one, which has not
// moved.
//
// **A recommendation carries zero authority.** It cites the metric it rests on and nothing it can
// execute; turning one into a change is the separate, human-approved strategy proposal
// (useStrategyProposals.ts). So there is no "apply" mutation here — only the list and the
// generate that appends to it.
import { computed, toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import type { CareerAnalytics, CareerRecommendationList } from '~/types/v2'
import { apiGet, apiPost } from '~/utils/api-client'
import { V2_ENDPOINTS } from '~/utils/endpoints'
import { invalidate, useApiQuery } from './useApiQuery'
import { useMutation } from './useMutation'

/** The prefix a fresh generation refreshes. */
const RECOMMENDATIONS_KEY = 'career-recommendations'

/**
 * This account's funnel report as of now, over one maturity horizon.
 *
 * `horizonDays` is optional — omitted, the server applies its own default window, and the key is
 * the bare `career-analytics`. Passed, it becomes a query param and part of the key, so two
 * windows are distinct cache entries rather than a silent overwrite (mirroring V1's `analytics:30`).
 */
export function useCareerAnalytics(horizonDays?: MaybeRefOrGetter<number | null>) {
  const horizon = computed(() => (horizonDays === undefined ? null : toValue(horizonDays)))
  return useApiQuery<CareerAnalytics>(
    computed(() => (horizon.value === null
      ? 'career-analytics'
      : `career-analytics:${horizon.value}`)),
    () => apiGet<CareerAnalytics>(horizon.value === null
      ? V2_ENDPOINTS.careerAnalytics
      : `${V2_ENDPOINTS.careerAnalytics}?horizon_days=${horizon.value}`),
  )
}

/** This account's recommendations, most recently created first — a surface to review. */
export function useRecommendationsQuery() {
  return useApiQuery<CareerRecommendationList>(RECOMMENDATIONS_KEY, () =>
    apiGet<CareerRecommendationList>(V2_ENDPOINTS.careerRecommendations))
}

/**
 * Compute the report and derive a fresh set of evidence-backed suggestions from it.
 *
 * A 201 that appends to the write-once store, so on success the recommendations list is
 * invalidated. The analytics key is deliberately left alone: the report is not mutated by a
 * generation, only read.
 */
export function useGenerateRecommendations() {
  return useMutation<void, CareerRecommendationList>(
    () => apiPost<CareerRecommendationList>(V2_ENDPOINTS.careerRecommendations),
    { onSuccess: () => invalidate(RECOMMENDATIONS_KEY) },
  )
}
