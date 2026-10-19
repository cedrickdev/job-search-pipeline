import { mountSuspended } from '@nuxt/test-utils/runtime'
import { ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'
import LlmConnectionsSettings from '~/components/LlmConnectionsSettings.vue'
import type { ClaudeTerminalSession } from '~/types/v2'
import { ApiError } from '~/utils/api-client'

const terminal = {
  session: ref<ClaudeTerminalSession | null>(null),
  pending: ref(false),
  error: ref<unknown>(null),
  connected: ref(false),
  create: vi.fn().mockResolvedValue(null),
  stop: vi.fn().mockResolvedValue(null),
  connect: vi.fn(), sendInput: vi.fn(), resize: vi.fn(), disconnect: vi.fn(),
}

vi.mock('~/composables/useClaudeTerminal', () => ({ useClaudeTerminal: () => terminal }))
vi.mock('~/composables/useLlmConnections', () => ({
  useLlmConnectionsQuery: () => ({ data: ref({ connections: [] }), status: ref('success'), error: ref(null) }),
  useLlmConnectionActions: () => ({
    create: { isPending: false, isSuccess: false, mutateAsync: vi.fn() },
    update: { isPending: false, mutateAsync: vi.fn() }, remove: { isPending: false, mutate: vi.fn() },
    setEnabled: { isPending: false, mutate: vi.fn() }, setDefault: { isPending: false, mutate: vi.fn() },
  }),
  useLlmConnectionHealth: () => ({ mutateAsync: vi.fn() }),
}))

afterEach(() => {
  terminal.session.value = null
  terminal.error.value = null
  terminal.create.mockClear()
})

describe('LlmConnectionsSettings terminal controls', () => {
  it('starts an isolated terminal from the settings action', async () => {
    const wrapper = await mountSuspended(LlmConnectionsSettings, {
      global: { stubs: { ClaudeTerminalPanel: true, LlmConnectionForm: true } },
    })

    await wrapper.get('button').trigger('click')

    expect(terminal.create).toHaveBeenCalledOnce()
  })

  it('renders a human terminal status instead of the raw ACTIVE state', async () => {
    terminal.session.value = { state: 'ACTIVE' } as ClaudeTerminalSession
    const wrapper = await mountSuspended(LlmConnectionsSettings, {
      global: { stubs: { ClaudeTerminalPanel: true, LlmConnectionForm: true } },
    })

    expect(wrapper.text()).toContain('Terminal ready')
    expect(wrapper.text()).not.toContain('ACTIVE')
  })

  it('replaces a raw terminal server error with a clear recovery action', async () => {
    terminal.error.value = new ApiError(500, 'Internal Server Error')
    const wrapper = await mountSuspended(LlmConnectionsSettings, {
      global: { stubs: { ClaudeTerminalPanel: true, LlmConnectionForm: true } },
    })

    expect(wrapper.text()).toContain('secure terminal service is unavailable')
    expect(wrapper.text()).not.toContain('Internal Server Error')
  })

  it('explains why a failed terminal session cannot be opened', async () => {
    terminal.session.value = {
      state: 'FAILED', safe_error_code: 'TERMINAL_UNAVAILABLE',
    } as ClaudeTerminalSession
    const wrapper = await mountSuspended(LlmConnectionsSettings, {
      global: { stubs: { ClaudeTerminalPanel: true, LlmConnectionForm: true } },
    })

    expect(wrapper.text()).toContain('secure terminal service is unavailable')
  })
})
