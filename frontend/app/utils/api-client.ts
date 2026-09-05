// The HTTP boundary. One place that knows how to talk to FastAPI, so the
// composables above it only deal in domain types.
//
// V1's webapp/src/api/client.ts wrapped `fetch` and threw `ApiError { status,
// detail }`; the components branch on that shape, and the tests assert it. Nuxt
// gives us `$fetch`, which throws a `FetchError` instead — different class,
// different fields, and it parses the body itself. Rather than teach every
// caller two error shapes, this module keeps V1's contract and reproduces its
// three quirks exactly:
//
//   1. an empty response body resolves to `null`, not a parse error;
//   2. a non-JSON body resolves to its raw text;
//   3. an error body's `detail` field is unwrapped, falling back to the whole
//      body when there is none (FastAPI's HTTPException shape).
//
// It uses `fetch` directly for that reason. `$fetch`'s conveniences — baseURL,
// automatic JSON, interceptors — are what get in the way here: parity with the
// old error surface is worth more during a migration than idiomatic Nuxt.
//
// Phase 4 added the two things a session-bearing client needs, both additive:
//
//   * `X-CSRF-Token` on every unsafe request, copied from the CSRF cookie. The
//     session cookie is `HttpOnly` and unreadable here by design; the CSRF cookie
//     is readable precisely so it can be echoed in this header, which is what the
//     backend compares against the digest stored with the session
//     (docs/AUTHENTICATION.md §CSRF). V1 routes ignore the header, so it is sent
//     unconditionally rather than per-surface.
//   * `ApiError.code`, because V2 error bodies are `{"error": …, "detail": …}` and
//     `detail` alone loses the machine-readable half. V1 bodies have no `error`
//     key, so their `code` is null and nothing about them changes.

/** The header the backend expects; `backend/app/api/dependencies.py` names it too. */
export const CSRF_HEADER = 'X-CSRF-Token'

// Both spellings the backend may have issued, most-trusted first. `cookie_secure`
// prefixes the name with `__Host-`, which a compromised sibling subdomain cannot
// set; the bare name only appears in the explicit local-HTTP deployment
// (`AuthSettings.for_local_http`). Preferring the prefixed one means a planted
// bare cookie cannot displace the real token.
const CSRF_COOKIE_NAMES = ['__Host-jobsearch_csrf', 'jobsearch_csrf'] as const

/** Methods that need no CSRF token, matching the backend's `SAFE_METHODS`. */
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

/**
 * Thrown for any non-2xx response. V1's two fields, unchanged, plus two.
 *
 * `code` is the V2 `error` slug (`"invalid_credentials"`, `"csrf_failed"`, …) and
 * is what callers should branch on; `detail` stays the human sentence it was.
 * `body` is the whole parsed payload, for the refusals that carry more than a
 * sentence — `account_locked` includes `locked_until`, `onboarding_incomplete`
 * includes the two counts, `validation_failed` includes the field list.
 */
export class ApiError extends Error {
  status: number
  detail: unknown
  code: string | null
  body: unknown

  constructor(status: number, detail: unknown, body: unknown = detail) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
    this.code = errorCode(body)
    this.body = body
  }
}

/** The `error` slug of a V2 refusal body, or null for V1's `{detail: …}` shape. */
function errorCode(body: unknown): string | null {
  if (body && typeof body === 'object' && 'error' in body) {
    const code = (body as { error: unknown }).error
    if (typeof code === 'string') return code
  }
  return null
}

/**
 * The CSRF cookie's current value, or null when no session issued one.
 *
 * Reads `document.cookie` rather than tracking the value in memory: the cookie is
 * the browser's copy, it can be cleared by a logout in another tab, and a cached
 * token would then be sent forever against a session that no longer exists.
 */
export function csrfToken(): string | null {
  if (typeof document === 'undefined') return null
  const jar = document.cookie.split(';').map(entry => entry.trim())
  for (const name of CSRF_COOKIE_NAMES) {
    const hit = jar.find(entry => entry.startsWith(`${name}=`))
    if (hit !== undefined) return decodeURIComponent(hit.slice(name.length + 1))
  }
  return null
}

async function parse(res: Response): Promise<unknown> {
  const text = await res.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

async function handle<T>(res: Response): Promise<T> {
  const body = await parse(res)
  if (!res.ok) {
    const detail
      = body && typeof body === 'object' && 'detail' in body
        ? (body as { detail: unknown }).detail
        : body
    throw new ApiError(res.status, detail, body)
  }
  return body as T
}

/**
 * The headers for one request: JSON, plus the CSRF token when it is needed.
 *
 * `same-origin` credentials are the `fetch` default and are spelled out because
 * this client is now carrying a session cookie: the app is served by the same
 * FastAPI process it calls (and by Nitro's `/api` proxy in development), so a
 * cross-origin request here would be a mistake rather than a case to support.
 */
function headers(method: string): HeadersInit {
  const base: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  }
  if (SAFE_METHODS.has(method)) return base
  const token = csrfToken()
  return token === null ? base : { ...base, [CSRF_HEADER]: token }
}

function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  return fetch(path, {
    method,
    credentials: 'same-origin',
    headers: headers(method),
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(handle<T>)
}

export function apiGet<T>(path: string): Promise<T> {
  return fetch(path, {
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  }).then(handle<T>)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>('POST', path, body)
}

export function apiPut<T>(path: string, body?: unknown): Promise<T> {
  return request<T>('PUT', path, body)
}

/** DELETE, whose 204 resolves to `null` through the empty-body rule above. */
export function apiDelete<T>(path: string): Promise<T> {
  return request<T>('DELETE', path)
}

/** POST a multipart body (audio blob for /api/transcribe). No JSON headers. */
export function apiPostForm<T>(path: string, form: FormData): Promise<T> {
  const token = csrfToken()
  return fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    // No `Content-Type`: the browser has to set it, boundary included.
    headers: token === null ? undefined : { [CSRF_HEADER]: token },
    body: form,
  }).then(handle<T>)
}
