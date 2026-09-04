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

/** Thrown for any non-2xx response. Mirrors V1's `ApiError` field for field. */
export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
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
    throw new ApiError(res.status, detail)
  }
  return body as T
}

export function apiGet<T>(path: string): Promise<T> {
  return fetch(path, { headers: { Accept: 'application/json' } }).then(handle<T>)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(handle<T>)
}

export function apiPut<T>(path: string, body?: unknown): Promise<T> {
  return fetch(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then(handle<T>)
}

/** POST a multipart body (audio blob for /api/transcribe). No JSON headers. */
export function apiPostForm<T>(path: string, form: FormData): Promise<T> {
  return fetch(path, { method: 'POST', body: form }).then(handle<T>)
}
