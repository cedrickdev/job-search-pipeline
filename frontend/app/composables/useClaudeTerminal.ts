import { ref } from 'vue'
import type { ClaudeTerminalSession } from '~/types/v2'
import { ApiError, apiDelete, apiPost } from '~/utils/api-client'
import { V2_ENDPOINTS } from '~/utils/endpoints'

export type { ClaudeTerminalSession }

const SESSIONS = V2_ENDPOINTS.claudeTerminalSessions

export function encodeResize(cols: number, rows: number) {
  if (!Number.isInteger(cols) || !Number.isInteger(rows)
    || cols < 1 || cols > 500 || rows < 1 || rows > 500) {
    throw new Error('terminal dimensions must be between 1 and 500')
  }
  return { type: 'resize' as const, cols, rows }
}

export function useClaudeTerminal() {
  const session = ref<ClaudeTerminalSession | null>(null)
  const pending = ref(false)
  const error = ref<unknown>(null)
  const connected = ref(false)
  let socket: WebSocket | null = null

  async function create() {
    pending.value = true; error.value = null
    try { session.value = await apiPost<ClaudeTerminalSession>(SESSIONS); return session.value }
    catch (caught) { error.value = caught; throw caught }
    finally { pending.value = false }
  }

  async function stop() {
    if (!session.value) return null
    pending.value = true; error.value = null
    try {
      session.value = await apiDelete<ClaudeTerminalSession>(`${SESSIONS}/${session.value.id}`)
      disconnect()
      error.value = null
      return session.value
    }
    catch (caught) { error.value = caught; throw caught }
    finally { pending.value = false }
  }

  function connect(onData: (data: Uint8Array) => void) {
    if (!session.value || session.value.state !== 'ACTIVE') throw new Error('active session required')
    disconnect()
    const target = new URL(session.value.stream_url, window.location.origin)
    target.protocol = target.protocol === 'https:' ? 'wss:' : 'ws:'
    const current = new WebSocket(target)
    socket = current
    current.binaryType = 'arraybuffer'
    current.onopen = () => {
      if (socket !== current) return
      connected.value = true
      error.value = null
    }
    const connectionFailed = () => {
      if (socket !== current) return
      connected.value = false
      error.value = new ApiError(503, 'TERMINAL_CONNECTION_CLOSED')
    }
    current.onclose = () => {
      connectionFailed()
      if (socket === current) socket = null
    }
    current.onerror = connectionFailed
    current.onmessage = event => {
      if (socket !== current) return
      if (event.data instanceof ArrayBuffer) onData(new Uint8Array(event.data))
    }
  }

  function sendInput(data: string) {
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data }))
  }
  function resize(cols: number, rows: number) {
    const frame = encodeResize(cols, rows)
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame))
  }
  function disconnect() {
    const previous = socket
    socket = null
    connected.value = false
    previous?.close()
  }

  return { session, pending, error, connected, create, stop, connect, sendInput, resize, disconnect }
}
