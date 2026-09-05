// Shared HTTP stub for the unit suite.
//
// `app/utils/api-client.ts` calls the global `fetch` directly (deliberately — see
// its header), so `fetch` is the seam these tests replace, exactly as V1's tests
// did with `vi.stubGlobal("fetch", ...)`. @nuxt/test-utils' `registerEndpoint`
// intercepts `$fetch`/ofetch and would therefore never be consulted here.
//
// The one deviation from V1 is deliberate: V1 stubbed a single response for every
// URL, so a component that fetched the wrong endpoint still got valid-looking
// data. This takes a route table and answers an unlisted URL with 404, recording
// it in `unmatched`, so "the component asked for something nobody stubbed" is
// visible instead of silently green.
import { vi } from 'vitest'

export interface Route {
  /** Matched against the request URL: substring, or regex for anything finer. */
  match: string | RegExp
  /** Restrict the route to one HTTP method; unset matches any. */
  method?: string
  status?: number
  /** Response body, JSON-encoded. Mutually exclusive with `sse`/`text`. */
  json?: unknown
  /** Raw body, sent as-is (used for the non-JSON parse path). */
  text?: string
  /** Stream these raw chunks as `text/event-stream`, like sse_starlette does. */
  sse?: string[]
  /** Last resort: build the Response yourself. */
  respond?: (url: string, init?: RequestInit) => Response | Promise<Response>
}

export interface RecordedCall {
  url: string
  method: string
  init?: RequestInit
}

export interface FetchStub {
  calls: RecordedCall[]
  /** Calls whose URL matched no route — should normally be empty. */
  unmatched: RecordedCall[]
  /** True when any call's URL contains `fragment`. */
  called: (fragment: string) => boolean
  /** Every call whose URL contains `fragment`. */
  callsTo: (fragment: string) => RecordedCall[]
  /** The JSON-parsed request body of the first call to `fragment`. */
  bodyOf: (fragment: string) => unknown
}

/** Build a streaming `Response` that emits `chunks` verbatim. */
export function sseResponse(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
  return new Response(body, {
    status,
    headers: { 'Content-Type': 'text/event-stream' },
  })
}

/** Statuses the Response constructor refuses to give a body to. */
const BODYLESS = new Set([204, 205, 304])

function build(route: Route, url: string, init?: RequestInit): Response | Promise<Response> {
  if (route.respond) return route.respond(url, init)
  if (route.sse) return sseResponse(route.sse, route.status ?? 200)
  const status = route.status ?? 200
  // `new Response("", {status: 204})` throws — a 204 may not carry a body at all,
  // even an empty string. The V2 deletes answer 204, so this is the shape they need.
  if (BODYLESS.has(status)) return new Response(null, { status })
  if (route.text !== undefined) return new Response(route.text, { status })
  return new Response(JSON.stringify(route.json ?? null), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

/**
 * Install a `fetch` that answers from `routes`, first match wins.
 * Returns a handle for asserting on what was requested.
 */
export function stubFetch(routes: Route[]): FetchStub {
  const calls: RecordedCall[] = []
  const unmatched: RecordedCall[] = []

  const impl = vi.fn(async (input: unknown, init?: RequestInit): Promise<Response> => {
    const url = typeof input === 'string' ? input : String((input as { url?: string })?.url ?? input)
    const method = (init?.method ?? 'GET').toUpperCase()
    const call: RecordedCall = { url, method, init }
    calls.push(call)

    const route = routes.find((r) => {
      if (r.method && r.method.toUpperCase() !== method) return false
      return typeof r.match === 'string' ? url.includes(r.match) : r.match.test(url)
    })
    if (!route) {
      unmatched.push(call)
      return new Response(JSON.stringify({ detail: `no route stubbed for ${method} ${url}` }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    return build(route, url, init)
  })

  vi.stubGlobal('fetch', impl)

  return {
    calls,
    unmatched,
    called: fragment => calls.some(c => c.url.includes(fragment)),
    callsTo: fragment => calls.filter(c => c.url.includes(fragment)),
    bodyOf: (fragment) => {
      const call = calls.find(c => c.url.includes(fragment))
      const body = call?.init?.body
      return typeof body === 'string' ? JSON.parse(body) : body
    },
  }
}
