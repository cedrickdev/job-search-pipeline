import { mount } from '@vue/test-utils'
import { nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import ClaudeTerminalPanel from '~/components/ClaudeTerminalPanel.vue'

const writes: Uint8Array[] = []
let terminalOutput: ((data: Uint8Array) => void) | undefined

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 120
    rows = 42
    loadAddon = vi.fn()
    open = vi.fn()
    dispose = vi.fn()
    write = (data: Uint8Array) => writes.push(data)
    onData = vi.fn()
  },
}))

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    fit = vi.fn()
  },
}))

function terminal(state = 'ACTIVE') {
  return {
    session: ref(state === 'ACTIVE' ? {
      id: '11111111-1111-4111-8111-111111111111', state,
      created_at: '2026-10-08T20:00:00Z', expires_at: '2026-10-08T21:00:00Z',
      ended_at: null, safe_error_code: null,
      stream_url: '/api/v2/settings/claude-terminal/sessions/11111111-1111-4111-8111-111111111111/stream',
    } : null),
    pending: ref(false),
    error: ref(null),
    connected: ref(false),
    create: vi.fn(),
    stop: vi.fn().mockResolvedValue(null),
    connect: vi.fn((output: (data: Uint8Array) => void) => { terminalOutput = output }),
    sendInput: vi.fn(),
    resize: vi.fn(),
    disconnect: vi.fn(),
  }
}

describe('ClaudeTerminalPanel', () => {
  it('opens the terminal after the asynchronous session creates its host element', async () => {
    const session = terminal('CREATING')
    const wrapper = mount(ClaudeTerminalPanel, { props: { terminal: session } })
    expect(session.connect).not.toHaveBeenCalled()
    session.session.value = terminal().session.value
    await nextTick()
    expect(wrapper.find('.claude-terminal__screen').exists()).toBe(true)
    expect(session.connect).toHaveBeenCalledOnce()
    wrapper.unmount()
  })
  it('provides a labelled terminal region and an accessible stop control', async () => {
    const session = terminal()
    const wrapper = mount(ClaudeTerminalPanel, { props: { terminal: session } })

    expect(wrapper.get('[role="region"][aria-label="Claude Code terminal"]')).toBeTruthy()
    await wrapper.get('button[aria-label="Stop terminal"]').trigger('click')

    expect(session.stop).toHaveBeenCalledOnce()
  })

  it('keeps terminal output outside the application HTML tree', () => {
    const session = terminal()
    const wrapper = mount(ClaudeTerminalPanel, { props: { terminal: session } })
    const unsafe = new TextEncoder().encode('<img src=x onerror=alert(1)>')

    terminalOutput?.(unsafe)

    expect(writes).toContainEqual(unsafe)
    expect(wrapper.html()).not.toContain('<img src=x')
  })

  it('offers a reconnect control when the active terminal socket is offline', async () => {
    const session = terminal()
    const wrapper = mount(ClaudeTerminalPanel, { props: { terminal: session } })

    await wrapper.get('button[aria-label="Reconnect terminal"]').trigger('click')

    expect(session.disconnect).toHaveBeenCalledOnce()
    expect(session.connect).toHaveBeenCalledTimes(2)
  })
})
