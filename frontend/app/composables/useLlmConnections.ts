// The `/api/v2/settings/llm` surface: a user's stored LLM connections — one read,
// five writes and a live health probe.
//
// A file of its own, next to useDocuments.ts, because it configures a different
// thing entirely. The evidence and document composables operate on the candidate's
// truth; this one operates on the platform's *plumbing* — which provider a task
// reaches, at which endpoint, with which credential (docs/LLM_CONNECTIONS.md). The
// two share the `/me`-style authorization (the owner is the session, never the
// path), but nothing else, so keeping them apart keeps each legible.
//
// Three things here are decisions rather than plumbing.
//
// **The credential is write-only, end to end.** `create` and `update` send an
// `api_key`; no response ever carries it back — a connection is only ever echoed as
// `has_api_key` (§13). The three-state edit `UpdateLLMConnectionRequest` documents is
// preserved verbatim: a field left unset is untouched, `api_key` rotates the key, and
// `remove_api_key` clears it. The page never round-trips a key through a form value,
// so a saved connection cannot leak its secret into the DOM.
//
// **Every write refreshes the one list key.** A create, edit, delete, toggle or
// default-change all alter the same `/connections` list — there is no per-connection
// detail query, because the list already carries whole connection objects — so the
// success handler invalidates the `llm:connections` prefix and the table re-reads in
// the router's own priority-then-id order. Marking one default clears the flag on the
// rest server-side, so the refreshed list shows the single default the invariant
// guarantees.
//
// **Health is a probe, not a stored fact.** `probe` is a `POST` that builds the
// connection's provider and reaches out; its answer is data (an `UNAVAILABLE` status a
// row renders), not an error, and it changes nothing, so it does *not* invalidate the
// list. It is a mutation for its pending flag — a row disables its "Test" button while
// the bytes are in flight — and the page keys each result by connection id itself
// (§37).
import type {
  CreateLLMConnectionRequest,
  LLMConnection,
  LLMConnectionHealth,
  LLMConnectionList,
  LLMProviderType,
  UpdateLLMConnectionRequest,
} from '~/types/v2'
import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from '~/utils/api-client'
import { V2_ENDPOINTS, llmConnection } from '~/utils/endpoints'
import { invalidate, useApiQuery } from './useApiQuery'
import { useMutation } from './useMutation'

/** The prefix every connection write refreshes. Expanded by `invalidate`. */
const CONNECTIONS_KEY = 'llm:connections'

/**
 * One connection as the settings form emits it — the union of what a create and an
 * edit each need, so the same form serves both.
 *
 * The page narrows it to the right request: a create reads `provider_type`, `enabled`
 * and `is_default` (which a `PATCH` cannot change — they are their own endpoints) and
 * ignores `remove_api_key`; an edit reads the rest and the three-state credential. The
 * credential travels one way only: `api_key` is a new value to set (or `null` to leave
 * it), `remove_api_key` clears a stored one, and no field ever carries a key *back* —
 * a saved connection is only ever `has_api_key` (§13).
 */
export interface ConnectionFormPayload {
  provider_type: LLMProviderType
  display_name: string
  base_url: string | null
  model: string | null
  api_key: string | null
  remove_api_key: boolean
  custom_headers: Record<string, string>
  enabled: boolean
  is_default: boolean
  priority: number
}

/**
 * This account's stored LLM connections, in the router's priority-then-id order.
 *
 * No `null` case, unlike the evidence and document reads: managing connections is not
 * gated on onboarding, so a signed-in account with none configured gets an empty list
 * (`{ connections: [] }`), which the settings page renders as "no connections yet"
 * rather than a "finish onboarding" prompt. A request without a session is a 401 the
 * page's auth guard handles, not a state this query models.
 */
export function useLlmConnectionsQuery() {
  return useApiQuery<LLMConnectionList>(CONNECTIONS_KEY, () =>
    apiGet<LLMConnectionList>(V2_ENDPOINTS.llmConnections))
}

/** A connection to edit, paired with the partial changes to apply to it. */
export interface UpdateArgs {
  connectionId: string
  changes: UpdateLLMConnectionRequest
}

/** A connection to toggle, paired with the state to set it to. */
export interface SetEnabledArgs {
  connectionId: string
  enabled: boolean
}

/**
 * The five writes on a user's connections: create, edit, delete, toggle and default.
 *
 * Grouped like `useEvidenceActions`, and for the same reason: the settings page needs
 * all of them, and one call site with separate mutations keeps their `isPending` flags
 * apart — toggling one connection must not disable the create form, and probing a
 * third must not look like a save. Every write refreshes `llm:connections` so the
 * table reflects the change without a reload.
 *
 * The refusals a page shows rather than swallows: 422 `llm_connection_invalid` when a
 * shape is incoherent (a CLI carrying a base URL, an API missing one), 409
 * `llm_secret_key_unavailable` when a credential is given but the deployment stored no
 * master key, and 404 `llm_connection_not_found` when an id is stale or not this
 * account's.
 */
export function useLlmConnectionActions() {
  const refresh = () => invalidate(CONNECTIONS_KEY)
  return {
    create: useMutation<CreateLLMConnectionRequest, LLMConnection>(
      body => apiPost<LLMConnection>(V2_ENDPOINTS.llmConnections, body),
      { onSuccess: refresh },
    ),
    update: useMutation<UpdateArgs, LLMConnection>(
      ({ connectionId, changes }) => apiPatch<LLMConnection>(
        llmConnection(V2_ENDPOINTS.llmConnection, connectionId), changes),
      { onSuccess: refresh },
    ),
    // DELETE resolves to `null` (204, empty body); a second delete of the same id is a
    // 404 `llm_connection_not_found`, so a stale list is told rather than misled.
    remove: useMutation<string, void>(
      connectionId => apiDelete<void>(
        llmConnection(V2_ENDPOINTS.llmConnection, connectionId)),
      { onSuccess: refresh },
    ),
    setEnabled: useMutation<SetEnabledArgs, LLMConnection>(
      ({ connectionId, enabled }) => apiPut<LLMConnection>(
        llmConnection(V2_ENDPOINTS.llmConnectionEnabled, connectionId), { enabled }),
      { onSuccess: refresh },
    ),
    // No body: the id in the path is the whole instruction. The service clears every
    // other default first, so the refreshed list carries exactly one.
    setDefault: useMutation<string, LLMConnection>(
      connectionId => apiPut<LLMConnection>(
        llmConnection(V2_ENDPOINTS.llmConnectionDefault, connectionId)),
      { onSuccess: refresh },
    ),
  }
}

/**
 * Probe one connection's live provider and return its health — data, never an error.
 *
 * A mutation for its pending flag (a row disables "Test" while the probe runs), not a
 * query: it is an action a button takes, and its result is not cached state — health
 * is deliberately never stored (§37), so this does not invalidate the list. A provider
 * that is down answers with an `UNAVAILABLE` status and a secret-free `detail` the row
 * renders; the platform composes that sentence, so a raw provider message (which could
 * echo a key) never reaches the UI. A misconfigured connection (a local base URL that
 * is not loopback) is the one case that rejects instead, as a 409 `provider_misconfigured`
 * caught before the probe — the page shows it the same way it shows an `UNAVAILABLE`.
 */
export function useLlmConnectionHealth() {
  return useMutation<string, LLMConnectionHealth>(connectionId =>
    apiPost<LLMConnectionHealth>(
      llmConnection(V2_ENDPOINTS.llmConnectionHealthcheck, connectionId)))
}
