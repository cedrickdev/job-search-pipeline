// Ported from webapp/src/api/chat.test.ts.
//
// `parseSseBuffer` was the most-tested piece of the V1 frontend and it crossed to
// Vue unchanged, so these assertions are the V1 ones. The added case is the final
// frame that arrives with no trailing blank line, which `streamChat` flushes
// after the reader drains — a real sse_starlette behaviour that V1 implemented
// and never covered.
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ChatEvent } from '~/utils/chat'
import { actionEndpoint, parseSseBuffer, streamChat } from '~/utils/chat'
import { ApiError } from '~/utils/api-client'
import { stubFetch } from '../support/http'

afterEach(() => vi.restoreAllMocks())

describe('parseSseBuffer', () => {
  it('splits frames, JSON-parses data, and keeps the trailing partial as rest', () => {
    const { events, rest } = parseSseBuffer(
      'event: token\ndata: "Hel"\n\nevent: token\ndata: "lo"\n\nevent: do',
    )
    expect(events).toEqual([
      { event: 'token', data: 'Hel' },
      { event: 'token', data: 'lo' },
    ])
    expect(rest).toBe('event: do')
  })

  it('handles \\r\\n separators and skips comment/ping lines', () => {
    const { events } = parseSseBuffer(
      ': ping - keepalive\r\n\r\nevent: done\r\ndata: {"text":"hi","mandate_ok":true}\r\n\r\n',
    )
    expect(events).toEqual([{ event: 'done', data: { text: 'hi', mandate_ok: true } }])
  })

  it('leaves an unparseable data payload as a raw string', () => {
    const { events } = parseSseBuffer('event: info\ndata: plain text\n\n')
    expect(events).toEqual([{ event: 'info', data: 'plain text' }])
  })
})

describe('streamChat', () => {
  it('POSTs the scope/message and dispatches parsed events across chunk boundaries', async () => {
    const http = stubFetch([{
      match: '/api/chat',
      sse: [
        'event: token\r\ndata: "He',
        'llo"\r\n\r\nevent: done\r\ndata: {"text":"Hello","session_id":null,"mandate_ok":true,"flags":[]}\r\n\r\n',
      ],
    }])

    const seen: ChatEvent[] = []
    await streamChat({ message: 'hi', scope: 'job', scope_id: 7 }, { onEvent: e => seen.push(e) })

    expect(http.calls[0]?.url).toBe('/api/chat')
    expect(http.calls[0]?.method).toBe('POST')
    expect(http.bodyOf('/api/chat')).toEqual({ message: 'hi', scope: 'job', scope_id: 7 })
    // The token frame is split mid-data across the two chunks; it must reassemble
    // into one "Hello" token, proving the buffer survives chunk boundaries.
    expect(seen).toEqual([
      { event: 'token', data: 'Hello' },
      { event: 'done', data: { text: 'Hello', session_id: null, mandate_ok: true, flags: [] } },
    ])
  })

  it('defaults scope_id to 0 for the global scope', async () => {
    const http = stubFetch([{ match: '/api/chat', sse: ['event: done\ndata: {"text":"ok"}\n\n'] }])
    await streamChat({ message: 'hi', scope: 'global' }, { onEvent: () => {} })
    expect(http.bodyOf('/api/chat')).toEqual({ message: 'hi', scope: 'global', scope_id: 0 })
  })

  it('flushes a final frame that arrived with no trailing blank line', async () => {
    stubFetch([{ match: '/api/chat', sse: ['event: done\ndata: {"text":"bye"}'] }])
    const seen: ChatEvent[] = []
    await streamChat({ message: 'x', scope: 'global' }, { onEvent: e => seen.push(e) })
    expect(seen).toEqual([{ event: 'done', data: { text: 'bye' } }])
  })

  it('throws ApiError when the response is not ok', async () => {
    stubFetch([{ match: '/api/chat', status: 404, json: { detail: 'job not found' } }])
    await expect(
      streamChat({ message: 'x', scope: 'job', scope_id: 1 }, { onEvent: () => {} }),
    ).rejects.toBeInstanceOf(ApiError)
  })
})

describe('actionEndpoint', () => {
  it('maps each wired action type to its typed endpoint + body', () => {
    expect(actionEndpoint({ type: 'set_status', job_id: 7, args: { status: 'applied' } }))
      .toEqual({ path: '/api/jobs/7/status', body: { status: 'applied', detail: undefined } })
    expect(actionEndpoint({ type: 'mark_applied', job_id: 7, args: { channel: 'linkedin' } }))
      .toEqual({ path: '/api/jobs/7/applied', body: { channel: 'linkedin' } })
    expect(actionEndpoint({ type: 'mark_applied', job_id: 7 }))
      .toEqual({ path: '/api/jobs/7/applied', body: { channel: 'manual' } })
    // regen defaults creativity to balanced when the copilot omits it…
    expect(actionEndpoint({ type: 'regen', job_id: 7, args: { notes: 'more metrics' } }))
      .toEqual({ path: '/api/jobs/7/regen', body: { notes: 'more metrics', creativity: 'balanced' } })
    // …and passes an inferred boldness level straight through.
    expect(actionEndpoint({ type: 'regen', job_id: 7, args: { notes: 'max JD match', creativity: 'bold' } }))
      .toEqual({ path: '/api/jobs/7/regen', body: { notes: 'max JD match', creativity: 'bold' } })
    expect(actionEndpoint({ type: 'skip', job_id: 7 })).toEqual({ path: '/api/jobs/7/skip' })
  })

  it('returns null for draft_followup', () => {
    // The backend does serve POST /api/jobs/{id}/draft_followup — FollowupsPanel
    // calls it — but the copilot action stays unwired, as in V1, so the card
    // renders with Confirm disabled instead of POSTing on the copilot's word.
    expect(actionEndpoint({ type: 'draft_followup', job_id: 7, args: { tone: 'warm' } })).toBeNull()
  })
})
