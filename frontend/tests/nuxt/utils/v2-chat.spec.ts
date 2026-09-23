// The career-chat transport and its two pure display helpers.
//
// `streamChatTurn` is the V2 turn's only network call, and it is deliberately outside
// `api-client.ts` because it reads an SSE body rather than one JSON document. The cases
// here are the ones that behaviour has to get right: a POST that carries the turn, events
// that reassemble across chunk boundaries, a final frame with no trailing blank line, and
// the pre-stream refusal (404/422) that arrives as an ordinary JSON error body — a throw,
// not an ERROR event.
//
// `describeChatAction` and `navigationRoute` are the phase's rule made testable: a card's
// words come from the *typed* action, never the model's prose, and a NAVIGATE resolves
// only to a route this app owns — the two targets it does not (MATCHES, INTERVIEW_PREP)
// are ignored, never followed, so a chat action can never be an open redirect.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '~/utils/api-client'
import { describeChatAction, navigationRoute, streamChatTurn } from '~/utils/v2-chat'
import type { ChatAction, ChatStreamEvent, NavigationTarget } from '~/types/v2'
import { stubFetch } from '../support/http'

afterEach(() => vi.restoreAllMocks())

const PATH = '/api/v2/chat/conversations/c1/messages'

/** One `data:`-framed event, exactly as the route serializes it. */
function frame(event: Partial<ChatStreamEvent> & { type: ChatStreamEvent['type'] }): string {
  const full: ChatStreamEvent = {
    type: event.type,
    text: event.text ?? null,
    message: event.message ?? null,
    proposals: event.proposals ?? [],
    error_code: event.error_code ?? null,
    error_detail: event.error_detail ?? null,
  }
  return `data: ${JSON.stringify(full)}\n\n`
}

describe('streamChatTurn', () => {
  it('POSTs the turn and reassembles events split across chunk boundaries', async () => {
    const completed = frame({ type: 'COMPLETED', text: 'Hello' })
    const cut = Math.floor(completed.length / 2)
    const http = stubFetch([{
      match: PATH,
      sse: [
        `${frame({ type: 'TOKEN', text: 'Hel' }).slice(0, -1)}`, // frame minus its last \n
        `\n${frame({ type: 'TOKEN', text: 'lo' })}${completed.slice(0, cut)}`,
        completed.slice(cut),
      ],
    }])

    const seen: ChatStreamEvent[] = []
    await streamChatTurn(PATH, 'hi there', { onEvent: e => seen.push(e) })

    expect(http.calls[0]?.method).toBe('POST')
    expect(http.bodyOf(PATH)).toEqual({ text: 'hi there' })
    expect(seen.map(e => e.type)).toEqual(['TOKEN', 'TOKEN', 'COMPLETED'])
    expect(seen[0]?.text).toBe('Hel')
    expect(seen[1]?.text).toBe('lo')
    expect(seen[2]?.text).toBe('Hello')
  })

  it('sends the SSE Accept header and a JSON content type', async () => {
    const http = stubFetch([{ match: PATH, sse: [frame({ type: 'COMPLETED' })] }])
    await streamChatTurn(PATH, 'x', { onEvent: () => {} })

    const headers = new Headers(http.calls[0]?.init?.headers as HeadersInit)
    expect(headers.get('Accept')).toBe('text/event-stream')
    expect(headers.get('Content-Type')).toBe('application/json')
  })

  it('flushes a final frame that arrived with no trailing blank line', async () => {
    const noBlankLine = frame({ type: 'COMPLETED', text: 'bye' }).trimEnd()
    stubFetch([{ match: PATH, sse: [noBlankLine] }])

    const seen: ChatStreamEvent[] = []
    await streamChatTurn(PATH, 'x', { onEvent: e => seen.push(e) })
    expect(seen).toHaveLength(1)
    expect(seen[0]?.type).toBe('COMPLETED')
    expect(seen[0]?.text).toBe('bye')
  })

  // The ownership 404 and the empty-message 422 are raised before the stream opens, so
  // they arrive as an ordinary JSON body and must throw the same ApiError every other
  // call does — the caller branches on `error.code`, not on an ERROR event.
  it('throws an ApiError with the code when the pre-stream response is not ok', async () => {
    stubFetch([{
      match: PATH,
      status: 404,
      json: { error: 'conversation_not_found', detail: 'no such thread' },
    }])

    await expect(streamChatTurn(PATH, 'x', { onEvent: () => {} }))
      .rejects.toMatchObject({ status: 404, code: 'conversation_not_found' })
  })

  it('surfaces a mid-turn provider fault as a terminal ERROR event, not a throw', async () => {
    stubFetch([{
      match: PATH,
      sse: [frame({ type: 'ERROR', error_code: 'provider_timeout', error_detail: 'timed out' })],
    }])

    const seen: ChatStreamEvent[] = []
    await expect(streamChatTurn(PATH, 'x', { onEvent: e => seen.push(e) })).resolves.toBeUndefined()
    expect(seen[0]?.type).toBe('ERROR')
    expect(seen[0]?.error_code).toBe('provider_timeout')
  })
})

describe('describeChatAction', () => {
  // Every label reads a typed, domain-safe field, never the model's prose. One case per
  // action kind, so a new kind added to the union without a branch fails the typecheck.
  const cases: Array<[ChatAction, string]> = [
    [{ kind: 'NAVIGATE', target: 'OPPORTUNITIES', opportunity_id: null }, 'Go to the opportunity map'],
    [{ kind: 'OPEN_INTERVIEW_PREP', opportunity_id: 'o1' }, 'Open interview preparation for this opportunity'],
    [{ kind: 'GENERATE_RESUME', opportunity_id: 'o1', target_language: null }, 'Tailor a résumé for this opportunity'],
    [{ kind: 'GENERATE_RESUME', opportunity_id: 'o1', target_language: 'fr' }, 'Tailor a résumé for this opportunity in fr'],
    [{ kind: 'GENERATE_COVER_LETTER', opportunity_id: 'o1', target_language: null }, 'Write a cover letter for this opportunity'],
    [{ kind: 'GENERATE_COVER_LETTER', opportunity_id: 'o1', target_language: 'de' }, 'Write a cover letter for this opportunity in de'],
    [{ kind: 'CREATE_APPLICATION', opportunity_id: 'o1' }, 'Open an application for this opportunity'],
    [{ kind: 'PREPARE_APPLICATION', application_id: 'a1' }, 'Prepare this application'],
    [{ kind: 'APPROVE_APPLICATION', application_id: 'a1' }, 'Approve this application'],
    [{ kind: 'SUBMIT_APPLICATION', application_id: 'a1' }, 'Submit this application'],
    [{ kind: 'CANCEL_APPLICATION', application_id: 'a1' }, 'Cancel this application'],
    [{ kind: 'SET_SEARCH_RADIUS', search_profile_id: 's1', radius_km: 25 }, 'Set the search radius to 25 km'],
    [{ kind: 'UPDATE_SEARCH_KEYWORDS', search_profile_id: 's1' } as ChatAction, 'Update the keywords on this saved search'],
  ]

  it.each(cases)('labels %o from its typed fields', (action, expected) => {
    expect(describeChatAction(action)).toBe(expected)
  })
})

describe('navigationRoute', () => {
  it('maps each target this app owns to its route', () => {
    expect(navigationRoute('OPPORTUNITIES')).toBe('/map')
    expect(navigationRoute('APPLICATIONS')).toBe('/applications')
    expect(navigationRoute('DOCUMENTS')).toBe('/documents')
    expect(navigationRoute('COMPANIES')).toBe('/companies')
    expect(navigationRoute('SETTINGS')).toBe('/settings')
  })

  // The two targets with no standalone V2 route resolve to null: the client stays put
  // rather than following an unrecognised target, which keeps NAVIGATE from being an
  // open redirect (api.d.ts NavigationTarget).
  it('returns null for a target it does not own a route for', () => {
    const unowned: NavigationTarget[] = ['MATCHES', 'INTERVIEW_PREP']
    for (const target of unowned) expect(navigationRoute(target)).toBeNull()
  })
})
