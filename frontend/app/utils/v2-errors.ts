// V2 refusals, as sentences a form can show.
//
// The backend answers `{"error": "<code>", "detail": "<sentence>"}` and says which
// half is which: the code is stable and meant to be branched on, the sentence is for
// a log or a network tab and "no client should parse it"
// (backend/app/api/errors.py). So the wording a user reads is decided here, in one
// place, from the code — not passed through from `detail`.
//
// That is not a translation layer for its own sake. Two of the codes need more than
// their sentence: `account_locked` carries the deadline, and `validation_failed`
// carries the field list. Both are read off `ApiError.body`, which is why the client
// keeps it.
//
// Deliberately absent from every message: the address that was submitted, and any
// hint of whether it exists. `invalid_credentials` is one sentence for "no such
// account" and "wrong password" alike, because the API answers the same way for both
// and a friendlier message here would undo that (docs/AUTHENTICATION.md §Enumeration).
import type { V2ErrorCode } from '~/types/v2'
import { ApiError } from '~/utils/api-client'

const MESSAGES: Record<V2ErrorCode, string> = {
  account_disabled: 'This account has been disabled.',
  // Replaced below by a message carrying the deadline; kept for the case where the
  // body arrives without one.
  account_locked: 'Too many failed sign-in attempts. Try again later.',
  // The application-engine refusals (§80-82). Each is a state the caller resolves, not
  // a request to reshape: an adapter fault, a channel the platform cannot drive, a
  // decision not yet made, a document not ready, a form that changed, an unanswerable
  // required field, an operation the state forbids, an unknown id, a duplicate, an
  // exhausted budget, or a send whose outcome could not be confirmed.
  application_adapter_error: 'This application could not be completed automatically. Try again or apply manually.',
  application_channel_unsupported: 'This posting has no automated application path. Apply to it yourself.',
  application_decision_missing: 'Decide on this opportunity before opening an application for it.',
  application_document_not_ready: 'A required document is not a finished version yet. Generate one first.',
  application_duplicate: 'You have already applied to this posting.',
  application_form_changed: 'The application form changed since it was prepared. Prepare it again.',
  application_missing_answer: 'A required question has no trustworthy answer, so this needs you.',
  application_not_actionable: 'This application cannot do that in its current state. Reload to see where it stands.',
  application_not_found: 'That application does not exist.',
  application_rate_limited: 'You have reached your application limit for now. Try again later.',
  application_submission_unknown: 'This application left the platform but could not be confirmed. Check before retrying.',
  // The artifact store could not return the bytes of a version it says is rendered —
  // an infrastructure fault (a 500), not something the user did or can fix.
  artifact_unavailable: 'This document could not be retrieved right now. Try again in a moment.',
  candidate_profile_not_found: 'No profile has been saved for this account yet.',
  // A provider cannot do what a task needs (structured output on a plain CLI, tools on
  // a bare completion endpoint). The fix is choosing a different provider, not a retry.
  capability_not_supported: 'This provider does not support what that task needs. Choose a different connection.',
  // A generated line cited an evidence id the profile does not hold. The candidate
  // never types ids, so this is a stale form rather than a mistake to correct inline.
  claim_cites_unknown_evidence: 'That claim refers to evidence that is no longer on file. Reload and try again.',
  company_not_found: 'That company is not in the directory.',
  conflict: 'That change conflicts with something already saved. Reload and retry.',
  // The prompt plus its history was larger than the model's context window.
  context_length_exceeded: 'That request was too long for this model to handle.',
  csrf_failed: 'This request could not be verified. Reload the page and try again.',
  database_unavailable: 'The service is temporarily unavailable. Try again in a moment.',
  document_not_found: 'That document does not exist.',
  document_not_rendered: 'This document has no finished version to download yet. Generate one first.',
  email_already_registered: 'An account already exists for that email address.',
  // The honest refusal: the profile carries too little evidence to build a truthful
  // document, and the answer is to add evidence, never to invent content.
  insufficient_evidence: 'There is not enough evidence on your profile to build this document yet. Add evidence first.',
  invalid_credentials: 'That email address and password do not match an account.',
  // The connection's fields do not make a coherent shape — a CLI carrying a base URL,
  // an API missing one. The `messages` on the body say which rule failed.
  llm_connection_invalid: 'These connection settings are not valid together.',
  llm_connection_not_found: 'That LLM connection no longer exists.',
  // A credential was submitted, but the deployment configured no key to encrypt it —
  // an operator decision, not something a user can fix from this form.
  llm_secret_key_unavailable: 'This deployment is not configured to store an API key. Ask an administrator to enable it.',
  not_authenticated: 'Your session has ended. Sign in again to continue.',
  onboarding_incomplete: 'Save a profile and at least one active search first.',
  // The provider's answer exceeded the adapter's hard cap and was cut off.
  output_limit_exceeded: 'This provider returned more than could be handled. Try again.',
  // A hosted API refused for want of a valid credential (a 401/403). The fix is the
  // key on this connection, not a retry.
  provider_auth_required: 'This provider rejected its credentials. Check the API key on this connection.',
  provider_cancelled: 'That request was cancelled before it finished.',
  // The provider's own safety filter refused the request or the completion.
  provider_content_filtered: 'This provider’s safety filter refused that request.',
  provider_internal_error: 'This provider failed unexpectedly. Try again in a moment.',
  // Required configuration is missing or invalid before any request — no base URL, a
  // local endpoint that is not loopback. Surfaces from a healthcheck of a bad shape.
  provider_misconfigured: 'This connection is not configured correctly. Check its endpoint and model.',
  provider_protocol_error: 'This provider returned something that could not be understood.',
  provider_rate_limited: 'This provider is rate-limiting requests. Try again shortly.',
  provider_timeout: 'This provider did not answer in time. Try again.',
  provider_unavailable: 'This provider could not be reached. Check that it is running.',
  search_profile_not_found: 'That saved search no longer exists.',
  // A resume was asked for a session the provider no longer holds.
  session_not_found: 'That session has expired on the provider. Try again.',
  // The model answered, but its JSON did not match the requested schema even after the
  // one repair attempt — the content is untrusted and dropped rather than half-parsed.
  structured_output_invalid: 'This provider returned an answer in the wrong format. Try again.',
  validation_failed: 'Some of the details below are not valid.',
}

const FALLBACK = 'Something went wrong. Try again.'

/** A refusal as one sentence, whatever it turns out to be. */
export function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    // A rejected `fetch` — offline, DNS, a dropped connection. There is no status
    // and no body to read, and saying so is more use than a bare "error".
    return error === null || error === undefined
      ? FALLBACK
      : 'The server could not be reached. Check your connection and try again.'
  }
  if (error.code === 'account_locked') return lockedMessage(error.body)
  if (error.code !== null && error.code in MESSAGES) {
    return MESSAGES[error.code as V2ErrorCode]
  }
  // A V1-shaped body, or a code this build does not know: `detail` is at least the
  // server's own words.
  return typeof error.detail === 'string' ? error.detail : FALLBACK
}

/**
 * The lockout message, with the deadline in the reader's own timezone.
 *
 * The deadline is disclosed on purpose: the API only answers 423 once the password
 * was *correct*, so the caller has proved ownership of the account and telling them
 * when to come back is help rather than disclosure (backend/app/api/errors.py).
 */
function lockedMessage(body: unknown): string {
  const until = readLockedUntil(body)
  if (until === null) return MESSAGES.account_locked
  return `Too many failed sign-in attempts. Try again after ${until}.`
}

function readLockedUntil(body: unknown): string | null {
  if (!body || typeof body !== 'object' || !('locked_until' in body)) return null
  const raw = (body as { locked_until: unknown }).locked_until
  if (typeof raw !== 'string') return null
  const at = new Date(raw)
  return Number.isNaN(at.getTime())
    ? null
    : at.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

/**
 * The rejected fields of a 422, keyed by field name.
 *
 * `loc` is a path — `["body", "languages", 0, "level"]` — and the last string in it
 * is the field a form can highlight. The values are the server's `msg`, which for a
 * validation error is a description of the rule rather than the rejected value: the
 * 422 body is stripped of `input` before it leaves the API, so there is nothing here
 * that could echo a password back onto the screen.
 */
export function fieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError) || error.code !== 'validation_failed') return {}
  const body = error.body
  if (!body || typeof body !== 'object' || !('errors' in body)) return {}
  const raw = (body as { errors: unknown }).errors
  if (!Array.isArray(raw)) return {}
  const found: Record<string, string> = {}
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object') continue
    const { loc, msg } = entry as { loc?: unknown, msg?: unknown }
    if (!Array.isArray(loc) || typeof msg !== 'string') continue
    const named = [...loc].reverse().find(part => typeof part === 'string' && part !== 'body')
    if (typeof named === 'string' && !(named in found)) found[named] = msg
  }
  return found
}
