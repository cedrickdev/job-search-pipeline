// The `/api/v2/applications` surface: the list, one application's audit trail, and
// the six writes that move an application through its lifecycle.
//
// A file of its own, next to useDocuments.ts, because it operates the *last* stage of
// the funnel — turning a decided opportunity into an audited application — and its
// rule is the one the whole engine exists for: nothing is submitted that a human (or
// an explicit autopilot policy) has not cleared, and the gate is re-checked at the
// moment of submission (docs/APPLICATION_ENGINE.md §1, §5). The UI never decides that;
// it shows the state the server returns and offers the one action that state allows.
//
// Three things here are decisions rather than plumbing.
//
// **Every write returns the application's new state, and refreshes the list.** A
// prepare, approve, submit or cancel is a transition, not an idempotent replace, so
// each is a mutation whose success invalidates the `applications` prefix — the same
// reason useDocuments.ts refreshes after a generation. The event trail for that
// application is invalidated too, because a transition always appends to it.
//
// **Create is idempotent and its failures are the caller's to read.** Opening an
// application twice for one target returns the first; a missing decision is a 409
// (`application_decision_missing`) and an already-submitted target a 409
// (`application_duplicate`). The composable does not swallow them — the page shows the
// sentence `v2-errors.ts` maps — because "decide first" and "already applied" are
// different next steps.
//
// **The trail is a lazy query.** A list row shows an application's state; its history
// is fetched only when a row is expanded, keyed per id, so opening one application's
// trail never loads them all.
import { toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import type {
  Application,
  ApplicationEventList,
  ApplicationList,
  CreateApplicationRequest,
} from '~/types/v2'
import { apiGet, apiPost } from '~/utils/api-client'
import { V2_ENDPOINTS, application } from '~/utils/endpoints'
import { invalidate, useApiQuery } from './useApiQuery'
import { useMutation } from './useMutation'

/** The prefix every application write refreshes. */
const APPLICATIONS_KEY = 'applications'

/** This account's applications, newest first. */
export function useApplicationsQuery() {
  return useApiQuery<ApplicationList>(APPLICATIONS_KEY, () =>
    apiGet<ApplicationList>(V2_ENDPOINTS.applications))
}

/**
 * One application's append-only audit trail, keyed per id.
 *
 * Lazy by default so a list does not fetch every application's history at once: a
 * page passes `enabled` true only for the row a user expanded. The key carries the id
 * so two open trails do not share a cache entry.
 */
export function useApplicationEventsQuery(
  applicationId: MaybeRefOrGetter<string>,
  options: { enabled?: MaybeRefOrGetter<boolean> } = {},
) {
  return useApiQuery<ApplicationEventList>(
    () => `application-events:${toValue(applicationId)}`,
    () => apiGet<ApplicationEventList>(
      application(V2_ENDPOINTS.applicationEvents, toValue(applicationId))),
    { enabled: options.enabled },
  )
}

/**
 * The six lifecycle writes, grouped so one call site holds them with separate pending
 * flags — a submit in flight must not disable the cancel button on another row.
 *
 * Each transition refreshes both the list and the acted-on application's trail, so a
 * new state and its new event appear together.
 */
export function useApplicationActions() {
  const refresh = (applicationId?: string) => {
    invalidate(APPLICATIONS_KEY)
    if (applicationId !== undefined) {
      invalidate(`application-events:${applicationId}`)
    }
  }
  return {
    create: useMutation<CreateApplicationRequest, Application>(
      body => apiPost<Application>(V2_ENDPOINTS.applications, body),
      { onSuccess: () => refresh() },
    ),
    prepare: useMutation<string, Application>(
      applicationId => apiPost<Application>(
        application(V2_ENDPOINTS.applicationPrepare, applicationId)),
      { onSuccess: result => refresh(result.id) },
    ),
    approve: useMutation<string, Application>(
      applicationId => apiPost<Application>(
        application(V2_ENDPOINTS.applicationApprove, applicationId)),
      { onSuccess: result => refresh(result.id) },
    ),
    submit: useMutation<string, Application>(
      applicationId => apiPost<Application>(
        application(V2_ENDPOINTS.applicationSubmit, applicationId)),
      { onSuccess: result => refresh(result.id) },
    ),
    cancel: useMutation<string, Application>(
      applicationId => apiPost<Application>(
        application(V2_ENDPOINTS.applicationCancel, applicationId)),
      { onSuccess: result => refresh(result.id) },
    ),
  }
}
