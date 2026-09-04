// Ported from webapp/src/routes/SettingsPage.test.tsx.
//
// The load-bearing case is "saves the full settings object": the PUT replaces the
// whole document server-side, so a save that only sent the fields the user touched
// would silently reset auto-apply policy. That is why the page copies the settings
// into a local form and sends it whole, and why the assertion below checks
// untouched fields rather than only the edited ones.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import SettingsPage from '~/pages/settings.vue'
import type { Route } from '../support/http'
import { stubFetch } from '../support/http'

const SETTINGS = {
  auto_apply: false,
  auto_apply_min_score: 85,
  auto_apply_daily_cap: 5,
  tailor_creativity: 'balanced',
  llm_backend: 'claude_cli',
  llm_base_url: '',
  llm_model: '',
  schedule_enabled: true,
  schedule_time: '08:00',
  schedule_cadence: 'daily',
}

const IDLE = { state: 'idle', kind: null, started_at: null, last_run: null }

/** GET settings + GET run status + echo any PUT, like the V1 stub did. */
function routes(extra: Route[] = []): Route[] {
  return [
    ...extra,
    { match: '/api/runs/status', method: 'GET', json: IDLE },
    { match: '/api/runs/', method: 'POST', status: 202, text: '' },
    {
      match: '/api/settings',
      method: 'PUT',
      respond: (_url, init) => new Response(
        JSON.stringify({ settings: JSON.parse(String(init?.body)), status: 'ok' }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    },
    { match: '/api/settings', method: 'GET', json: { settings: SETTINGS, status: 'missing' } },
  ]
}

// `VueWrapper`, not `Awaited<ReturnType<typeof mountSuspended>>`: that resolves
// to `any` through the generic, which unties every helper below from the DOM API.
type Wrapper = VueWrapper

function labelled(wrapper: Wrapper, label: string) {
  return wrapper.get(`[aria-label="${label}"]`)
}

function buttonNamed(wrapper: Wrapper, label: RegExp) {
  const found = wrapper.findAll('button').find(b => label.test(b.text()))
  if (!found) throw new Error(`no button matching ${label}`)
  return found
}

describe('settings page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('hides local-model fields for the Claude CLI backend, shows them for Ollama', async () => {
    stubFetch(routes())
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    expect(wrapper.text()).toContain('Copilot model')
    // Default backend is the local Claude CLI -> no endpoint/model fields.
    expect(wrapper.find('[aria-label="Local endpoint"]').exists()).toBe(false)

    await labelled(wrapper, 'Copilot backend').setValue('ollama')
    expect(wrapper.find('[aria-label="Local endpoint"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Model name"]').exists()).toBe(true)
  })

  it('saves the full settings object (chosen backend + untouched auto-apply fields)', async () => {
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    await labelled(wrapper, 'Copilot backend').setValue('lmstudio')
    await labelled(wrapper, 'Model name').setValue('qwen3:8b')
    await buttonNamed(wrapper, /save settings/i).trigger('click')
    await flushPromises()

    const puts = http.callsTo('/api/settings').filter(c => c.method === 'PUT')
    expect(puts.length).toBe(1)
    const body = JSON.parse(String(puts[0]!.init?.body)) as Record<string, unknown>
    expect(body.llm_backend).toBe('lmstudio')
    expect(body.llm_model).toBe('qwen3:8b')
    // The 4 auto-apply fields are sent unchanged, so a save never silently resets them.
    expect(body.auto_apply).toBe(false)
    expect(body.auto_apply_min_score).toBe(85)
    expect(body.auto_apply_daily_cap).toBe(5)
    expect(body.tailor_creativity).toBe('balanced')
  })

  it('persists schedule changes through the settings save', async () => {
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    await labelled(wrapper, 'Schedule cadence').setValue('weekdays')
    await labelled(wrapper, 'Schedule time').setValue('09:30')
    await buttonNamed(wrapper, /save settings/i).trigger('click')
    await flushPromises()

    const body = JSON.parse(String(http.callsTo('/api/settings')
      .find(c => c.method === 'PUT')!.init?.body)) as Record<string, unknown>
    expect(body.schedule_cadence).toBe('weekdays')
    expect(body.schedule_time).toBe('09:30')
    expect(body.schedule_enabled).toBe(true)
  })

  it('keeps the numeric policy fields numeric', async () => {
    // Not a V1 case. `v-model.number` is what stops "90" reaching a backend that
    // validates an int; a plain v-model would send a string and 422.
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    await labelled(wrapper, 'Minimum score').setValue('90')
    await labelled(wrapper, 'Daily cap').setValue('8')
    await buttonNamed(wrapper, /save settings/i).trigger('click')
    await flushPromises()

    const body = JSON.parse(String(http.callsTo('/api/settings')
      .find(c => c.method === 'PUT')!.init?.body)) as Record<string, unknown>
    expect(body.auto_apply_min_score).toBe(90)
    expect(body.auto_apply_daily_cap).toBe(8)
  })

  it('confirms a successful save', async () => {
    stubFetch(routes())
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    expect(wrapper.text()).not.toContain('Saved.')
    await buttonNamed(wrapper, /save settings/i).trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Saved.')
  })

  it('shows the server detail when a save is rejected', async () => {
    stubFetch(routes([{
      match: '/api/settings',
      method: 'PUT',
      status: 400,
      json: { detail: 'llm_base_url must be a local address' },
    }]))
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    await buttonNamed(wrapper, /save settings/i).trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('llm_base_url must be a local address')
    expect(wrapper.text()).not.toContain('Saved.')
  })

  it('triggers a discovery run from the Run now button', async () => {
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    await buttonNamed(wrapper, /run now \(discovery\)/i).trigger('click')
    await flushPromises()

    expect(http.callsTo('/api/runs/discover').map(c => c.method)).toEqual(['POST'])
  })

  it('triggers a full pipeline run from its own button', async () => {
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    await buttonNamed(wrapper, /run full pipeline now/i).trigger('click')
    await flushPromises()

    expect(http.callsTo('/api/runs/full').map(c => c.method)).toEqual(['POST'])
  })

  it('disables the run buttons and shows progress while a run is active', async () => {
    stubFetch(routes([{
      match: '/api/runs/status',
      method: 'GET',
      json: {
        state: 'running',
        kind: 'full',
        started_at: '2026-06-17T08:00:00+00:00',
        last_run: null,
      },
    }]))
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    const running = buttonNamed(wrapper, /Running…/)
    expect(running.text()).toContain('Running… (started 08:00)')
    expect(running.attributes('disabled')).toBeDefined()
    expect(buttonNamed(wrapper, /run full pipeline now/i).attributes('disabled')).toBeDefined()
  })
})
