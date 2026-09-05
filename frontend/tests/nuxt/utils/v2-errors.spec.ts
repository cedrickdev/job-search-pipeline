// The refusal-to-sentence layer. One test per rule the module claims.
//
// These are unit tests on purpose: every form in the app renders `errorMessage`,
// and asserting the wording through four mounted pages would test Vue instead of
// the mapping. The two rules worth the most here are the ones with a security
// argument behind them — the same sentence for both halves of a bad login, and no
// echo of a rejected value — so they are asserted directly rather than implied.
import { describe, expect, it } from 'vitest'
import { ApiError } from '~/utils/api-client'
import { errorMessage, fieldErrors } from '~/utils/v2-errors'

/** A refusal as the API sends it: `{error, detail}` plus whatever it carries. */
function refusal(status: number, code: string, extra: Record<string, unknown> = {}) {
  const body = { error: code, detail: `server sentence for ${code}`, ...extra }
  return new ApiError(status, body.detail, body)
}

const CODES = [
  'account_disabled',
  'account_locked',
  'candidate_profile_not_found',
  'conflict',
  'csrf_failed',
  'database_unavailable',
  'email_already_registered',
  'invalid_credentials',
  'not_authenticated',
  'onboarding_incomplete',
  'search_profile_not_found',
  'validation_failed',
]

describe('errorMessage', () => {
  // The list is backend/app/api/errors.py's closed set. A slug added there without
  // a sentence here would fall through to the server's own `detail`, which is
  // documented as "no client should parse it" — and is not written for a reader.
  it('has its own sentence for every V2 error code', () => {
    for (const code of CODES) {
      const message = errorMessage(refusal(400, code))
      expect(message, code).not.toContain('server sentence')
      expect(message, code).not.toBe('Something went wrong. Try again.')
      expect(message.endsWith('.'), code).toBe(true)
    }
  })

  // docs/AUTHENTICATION.md §Enumeration: the API answers identically for an unknown
  // address and a wrong password, and this layer must not undo that.
  it('says the same thing whichever half of a login was wrong', () => {
    const unknownAccount = refusal(401, 'invalid_credentials', { detail: 'no such user' })
    const wrongPassword = refusal(401, 'invalid_credentials', { detail: 'bad password' })

    expect(errorMessage(unknownAccount)).toBe(errorMessage(wrongPassword))
    expect(errorMessage(wrongPassword)).not.toMatch(/no such|unknown|exists|user/i)
  })

  it('puts the lockout deadline in the message, in local time', () => {
    const until = '2026-01-02T10:00:00Z'
    const expected = new Date(until)
      .toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })

    const message = errorMessage(refusal(423, 'account_locked', { locked_until: until }))

    expect(message).toContain(expected)
  })

  it('falls back to the plain lockout sentence when no deadline is given', () => {
    const message = errorMessage(refusal(423, 'account_locked'))
    expect(message).toBe('Too many failed sign-in attempts. Try again later.')
  })

  it('ignores an unparseable deadline rather than printing it', () => {
    const message = errorMessage(refusal(423, 'account_locked', { locked_until: 'soon' }))
    expect(message).toBe('Too many failed sign-in attempts. Try again later.')
    expect(message).not.toContain('soon')
  })

  // A rejected `fetch` — offline, DNS, a dropped connection — has no status and no
  // body, and "check your connection" is the only useful thing to say.
  it('describes a transport failure as one', () => {
    expect(errorMessage(new TypeError('Failed to fetch')))
      .toBe('The server could not be reached. Check your connection and try again.')
  })

  it('falls back for a nullish failure', () => {
    expect(errorMessage(null)).toBe('Something went wrong. Try again.')
    expect(errorMessage(undefined)).toBe('Something went wrong. Try again.')
  })

  // A build talking to a newer backend: the slug is unknown, and the server's own
  // words are better than a shrug.
  it('uses the server detail for a code this build does not know', () => {
    const error = new ApiError(400, 'a new rule refused this', {
      error: 'some_future_code',
      detail: 'a new rule refused this',
    })
    expect(errorMessage(error)).toBe('a new rule refused this')
  })

  it('falls back when an unknown code carries no readable detail', () => {
    expect(errorMessage(new ApiError(500, { nested: true }, { error: 'x', detail: { nested: true } })))
      .toBe('Something went wrong. Try again.')
  })

  // V1's 28 paths answer `{detail: …}` with no `error` key, and their pages show
  // that sentence today.
  it('passes a V1 detail through unchanged', () => {
    expect(errorMessage(new ApiError(404, 'Job not found'))).toBe('Job not found')
  })
})

describe('fieldErrors', () => {
  function validation(errors: unknown[]) {
    const body = { error: 'validation_failed', detail: 'invalid', errors }
    return new ApiError(422, 'invalid', body)
  }

  it('keys each message by the last named part of its loc path', () => {
    const fields = fieldErrors(validation([
      { loc: ['body', 'display_name'], msg: 'too short' },
      { loc: ['body', 'languages', 0, 'level'], msg: 'not a valid level' },
    ]))

    expect(fields).toEqual({ display_name: 'too short', level: 'not a valid level' })
  })

  it('keeps the first message when two errors name the same field', () => {
    const fields = fieldErrors(validation([
      { loc: ['body', 'areas', 0, 'country'], msg: 'first' },
      { loc: ['body', 'areas', 1, 'country'], msg: 'second' },
    ]))

    expect(fields.country).toBe('first')
  })

  it('skips entries that are not shaped like a validation error', () => {
    const fields = fieldErrors(validation([
      null,
      'nonsense',
      { loc: 'not-an-array', msg: 'ignored' },
      { loc: ['body', 'email'], msg: 42 },
      { loc: ['body'], msg: 'no field to blame' },
      { loc: ['body', 'password'], msg: 'kept' },
    ]))

    expect(fields).toEqual({ password: 'kept' })
  })

  /**
   * The 422 body is stripped of `input` before it leaves the API, so a rejected
   * password can never reach a screen. This asserts the client would not surface it
   * even if a future body carried one — the field list is built from `msg` alone.
   */
  it('never surfaces a rejected value, only the rule', () => {
    const fields = fieldErrors(validation([
      { loc: ['body', 'password'], msg: 'too short', input: 'hunter2' },
    ]))

    expect(fields).toEqual({ password: 'too short' })
    expect(JSON.stringify(fields)).not.toContain('hunter2')
  })

  it('is empty for anything that is not a validation failure', () => {
    expect(fieldErrors(refusal(401, 'invalid_credentials'))).toEqual({})
    expect(fieldErrors(new ApiError(422, 'invalid', { error: 'validation_failed' }))).toEqual({})
    expect(fieldErrors(new TypeError('offline'))).toEqual({})
    expect(fieldErrors(null)).toEqual({})
  })
})
