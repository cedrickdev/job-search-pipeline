import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiDelete, apiPost } from '~/utils/api-client'
import { encodeResize, useClaudeTerminal } from '~/composables/useClaudeTerminal'

vi.mock('~/utils/api-client', async (importOriginal) => ({
  ...await importOriginal<typeof import('~/utils/api-client')>(),
  apiDelete: vi.fn(),
  apiPost: vi.fn(),
}))

const activeSession = {
  id: '11111111-1111-4111-8111-111111111111',
  state: 'ACTIVE',
  created_at: '2026-10-08T20:00:00Z',
  expires_at: '2026-10-08T21:00:00Z',
  ended_at: null,
  safe_error_code: null,
  stream_url: '/api/v2/settings/claude-terminal/sessions/11111111-1111-4111-8111-111111111111/stream',
}

const sockets: FakeWebSocket[] = []

class FakeWebSocket {
  static OPEN = 1
  readyState = FakeWebSocket.OPEN
  binaryType = ''
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: (() => void) | null = null
  close = vi.fn()
  send = vi.fn()

  constructor(_url: URL) {
    sockets.push(this)
  }
}

afterEach(() => {
  sockets.length = 0
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('useClaudeTerminal', () => {
  it('keeps the replacement connection usable when the previous socket closes late', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.mocked(apiPost).mockResolvedValue(activeSession)
    const terminal = useClaudeTerminal()
    await terminal.create()
    terminal.connect(() => undefined)
    const previous = sockets[0]!
    terminal.connect(() => undefined)
    sockets[1]!.onopen?.()
    previous.onclose?.()
    terminal.sendInput('hello\r')

    expect(terminal.connected.value).toBe(true)
    expect(sockets[1]!.send).toHaveBeenCalledWith(JSON.stringify({ type: 'input', data: 'hello\r' }))
  })

  it('reports an unexpected disconnect and clears the error after reconnecting', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.mocked(apiPost).mockResolvedValue(activeSession)
    const terminal = useClaudeTerminal()
    await terminal.create()
    terminal.connect(() => undefined)
    sockets[0]!.onopen?.()
    sockets[0]!.onclose?.()
    expect(terminal.error.value).not.toBeNull()
    terminal.connect(() => undefined)
    sockets[1]!.onopen?.()
    expect(terminal.error.value).toBeNull()
    terminal.disconnect()
    sockets[1]!.onclose?.()
    expect(terminal.error.value).toBeNull()
  })

  it('refuses to open a WebSocket without an active terminal session', () => {
    const terminal = useClaudeTerminal()

    expect(() => terminal.connect(() => undefined)).toThrow('active session required')
  })

  it('rejects terminal dimensions outside the server bounds', () => {
    expect(() => encodeResize(0, 42)).toThrow('terminal dimensions must be between 1 and 500')
    expect(() => encodeResize(120, 501)).toThrow('terminal dimensions must be between 1 and 500')
    expect(encodeResize(120, 42)).toEqual({ type: 'resize', cols: 120, rows: 42 })
  })

  it('closes the live socket after stopping the owned terminal session', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.mocked(apiPost).mockResolvedValue(activeSession)
    vi.mocked(apiDelete).mockResolvedValue({ ...activeSession, state: 'TERMINATED' })
    const terminal = useClaudeTerminal()

    await terminal.create()
    terminal.connect(() => undefined)
    sockets[0]!.onopen?.()
    await terminal.stop()

    expect(apiDelete).toHaveBeenCalledWith(`/api/v2/settings/claude-terminal/sessions/${activeSession.id}`)
    expect(sockets[0]!.close).toHaveBeenCalledOnce()
    expect(terminal.connected.value).toBe(false)
  })

  it('does not report a connection error when the server closes the stream during a requested stop', async () => {
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.mocked(apiPost).mockResolvedValue(activeSession)
    vi.mocked(apiDelete).mockImplementation(async () => {
      sockets[0]!.onclose?.()
      return { ...activeSession, state: 'TERMINATED' }
    })
    const terminal = useClaudeTerminal()
    await terminal.create()
    terminal.connect(() => undefined)
    sockets[0]!.onopen?.()
    await terminal.stop()
    expect(terminal.session.value?.state).toBe('TERMINATED')
    expect(terminal.error.value).toBeNull()
  })
})
