// The `/api/v2` outcome surface: the "observe" link of the spine, recording the real world against
// an application. An outcome is a fact about the hiring process — an acknowledgement, a screen, an
// interview, an offer, a rejection — and Phase 15's foundational separation is enforced by absence:
// nothing here reads or writes an `ApplicationState`. A recruiter's "no" is a `REJECTED` outcome and
// never a failed execution (docs/CAREER_INTELLIGENCE.md §observe, §the separation).
//
// Three decisions worth naming.
//
// **The timeline is a lazy, per-application query.** A row on the applications page shows an
// application's state; its outcome history is fetched only when the row is expanded, keyed per id,
// so opening one application's timeline never loads them all — the same shape as the audit trail.
//
// **Every write refreshes the timeline.** Record, correct and retract each append to or restate the
// record, so each invalidates the `application-outcomes` prefix. The prefix (not one exact key) is
// invalidated because a correction or retraction is keyed by the *outcome* id and does not carry the
// application id — refreshing every open timeline is both correct and cheap when at most one is open.
//
// **A correction supersedes, a retraction flips — neither deletes.** The list keeps every status, so
// the composable never filters; the timeline renders `SUPERSEDED` and `RETRACTED` rows alongside
// effective ones, because "we believed this, then took it back" is part of the audit.
import { toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import type {
  ApplicationOutcome,
  ApplicationOutcomeList,
  CorrectOutcomeRequest,
  RecordOutcomeRequest,
} from '~/types/v2'
import { apiGet, apiPost } from '~/utils/api-client'
import { V2_ENDPOINTS, application, outcome as outcomePath } from '~/utils/endpoints'
import { invalidate, useApiQuery } from './useApiQuery'
import { useMutation } from './useMutation'

/** The prefix every outcome write refreshes. */
const OUTCOMES_KEY = 'application-outcomes'

/**
 * One application's outcomes oldest-first, keyed per id.
 *
 * Lazy by default so a list does not fetch every application's timeline at once: a page passes
 * `enabled` true only for the row a user expanded. The key carries the id so two open timelines do
 * not share a cache entry.
 */
export function useApplicationOutcomesQuery(
  applicationId: MaybeRefOrGetter<string>,
  options: { enabled?: MaybeRefOrGetter<boolean> } = {},
) {
  return useApiQuery<ApplicationOutcomeList>(
    () => `${OUTCOMES_KEY}:${toValue(applicationId)}`,
    () => apiGet<ApplicationOutcomeList>(
      application(V2_ENDPOINTS.applicationOutcomes, toValue(applicationId))),
    { enabled: options.enabled },
  )
}

/** The argument to record: which application, and the milestone body. */
export interface RecordOutcomeArgs {
  applicationId: string
  body: RecordOutcomeRequest
}

/** The argument to correct: which outcome to supersede, and the corrected body. */
export interface CorrectOutcomeArgs {
  outcomeId: string
  body: CorrectOutcomeRequest
}

/**
 * The three outcome writes, grouped so one call site holds them with separate pending flags.
 *
 * Record opens a new milestone (idempotent by its derived id, so a double-click collapses onto one
 * row); correct supersedes a mistaken one with a new outcome pointing back at it; retract flips one
 * to `RETRACTED`. Each refreshes the timeline. None can move the Phase 12 execution state — that is
 * the whole point of a separate outcome record.
 */
export function useOutcomeActions() {
  const refresh = () => invalidate(OUTCOMES_KEY)
  return {
    record: useMutation<RecordOutcomeArgs, ApplicationOutcome>(
      ({ applicationId, body }) => apiPost<ApplicationOutcome>(
        application(V2_ENDPOINTS.applicationOutcomes, applicationId), body),
      { onSuccess: () => refresh() },
    ),
    correct: useMutation<CorrectOutcomeArgs, ApplicationOutcome>(
      ({ outcomeId, body }) => apiPost<ApplicationOutcome>(
        outcomePath(V2_ENDPOINTS.outcomeCorrect, outcomeId), body),
      { onSuccess: () => refresh() },
    ),
    retract: useMutation<string, ApplicationOutcome>(
      outcomeId => apiPost<ApplicationOutcome>(
        outcomePath(V2_ENDPOINTS.outcomeRetract, outcomeId)),
      { onSuccess: () => refresh() },
    ),
  }
}
