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
  candidate_profile_not_found: 'No profile has been saved for this account yet.',
  company_not_found: 'That company is not in the directory.',
  conflict: 'That change conflicts with something already saved. Reload and retry.',
  csrf_failed: 'This request could not be verified. Reload the page and try again.',
  database_unavailable: 'The service is temporarily unavailable. Try again in a moment.',
  email_already_registered: 'An account already exists for that email address.',
  invalid_credentials: 'That email address and password do not match an account.',
  not_authenticated: 'Your session has ended. Sign in again to continue.',
  onboarding_incomplete: 'Save a profile and at least one active search first.',
  search_profile_not_found: 'That saved search no longer exists.',
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
