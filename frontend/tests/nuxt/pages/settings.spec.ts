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
    { match: '/api/v2/me/morning-schedule', method: 'GET', json: { enabled: false, local_time: '08:00', cadence: 'daily' } },
    { match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [] } },
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
    document.cookie = 'app_locale=en; path=/; max-age=0'
  })

  it.each([
    ['en', 'CV and letter ready; not submitted'],
    ['fr', 'CV et lettre prêts ; candidature non envoyée'],
  ])('shows exact prepared version IDs without claiming a submission in %s', async (locale, label) => {
    document.cookie = `app_locale=${locale}; path=/`
    stubFetch(routes([{
      match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [{
        run_id: '55555555-5555-4555-8555-555555555555',
        search_profile_id: '33333333-3333-4333-8333-333333333333',
        status: 'SUCCEEDED', seen_count: 1, matched_count: 1,
        source_health: [], warning_codes: [],
        preparations: [{
          opportunity_id: '11111111-1111-4111-8111-111111111111',
          stage: 'DOCUMENTS_RENDERED', reason_code: null,
          resume_version_id: '22222222-2222-4222-8222-222222222222',
          cover_letter_version_id: '44444444-4444-4444-8444-444444444444',
          updated_at: '2026-10-13T08:00:00Z',
        }],
      }] },
    }]))
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    expect(wrapper.get('[data-testid="v2-morning-run"]').text()).toContain(label)
    const links = wrapper.get('[data-testid="v2-morning-run"]').findAll('a')
    expect(links.some(link => link.attributes('href') === '/api/v2/documents/versions/22222222-2222-4222-8222-222222222222/download')).toBe(true)
    expect(links.some(link => link.attributes('href') === '/api/v2/documents/versions/44444444-4444-4444-8444-444444444444/download')).toBe(true)
    wrapper.unmount()
  })

  it.each([
    ['en', 'APPROVED', null, 'LIVE_SUBMISSIONS_DISABLED', 'Approved but live sending is disabled'],
    ['fr', 'APPROVED', null, 'LIVE_SUBMISSIONS_DISABLED', 'Approuvée mais l’envoi réel est désactivé'],
    ['en', 'APPROVED', 'QUEUED', null, 'Queued for browser submission; not sent'],
    ['fr', 'APPROVED', 'QUEUED', null, 'En attente d’envoi par navigateur ; non envoyée'],
    ['en', 'SUBMISSION_STATE_UNKNOWN', null, 'SUBMISSION_STATE_UNKNOWN', 'Submission unconfirmed; do not retry automatically'],
    ['fr', 'SUBMISSION_STATE_UNKNOWN', null, 'SUBMISSION_STATE_UNKNOWN', 'Envoi non confirmé ; ne pas relancer automatiquement'],
    ['en', 'SUBMITTED', 'SUCCEEDED', null, 'Submission confirmed'],
    ['fr', 'SUBMITTED', 'SUCCEEDED', null, 'Candidature confirmée'],
  ])('shows application progress and audit link in %s for %s', async (locale, state, task, blocker, expected) => {
    document.cookie = `app_locale=${locale}; path=/`
    stubFetch(routes([{
      match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [{
        run_id: '55555555-5555-4555-8555-555555555555',
        search_profile_id: '33333333-3333-4333-8333-333333333333',
        status: 'SUCCEEDED', seen_count: 1, matched_count: 1,
        source_health: [], warning_codes: [],
        preparations: [{
          opportunity_id: '11111111-1111-4111-8111-111111111111',
          stage: 'DOCUMENTS_RENDERED', updated_at: '2026-10-13T08:00:00Z',
          application_id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
          application_state: state, submission_task_status: task,
          application_blocker_code: blocker,
        }],
      }] },
    }]))
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    const section = wrapper.get('[data-testid="v2-morning-run"]')
    expect(section.text()).toContain(expected)
    expect(section.get('a[href="/applications?application=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"]').text())
      .toContain(locale === 'fr' ? 'Historique' : 'history')
    wrapper.unmount()
  })

  it('announces a terminal provider failure instead of a CV still in progress', async () => {
    stubFetch(routes([{
      match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [{
        run_id: '55555555-5555-4555-8555-555555555555',
        search_profile_id: '33333333-3333-4333-8333-333333333333',
        status: 'PARTIAL', seen_count: 1, matched_count: 1,
        source_health: [], warning_codes: [],
        preparations: [{
          opportunity_id: '11111111-1111-4111-8111-111111111111',
          stage: 'CV_RENDERED', resume_version_id: '22222222-2222-4222-8222-222222222222',
          cover_letter_version_id: null, task_status: 'DEAD_LETTERED',
          task_failure_code: 'PROVIDER_UNAVAILABLE', updated_at: '2026-10-13T08:00:00Z',
        }], preparations_truncated: false,
      }] },
    }]))
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    expect(wrapper.get('[data-testid="v2-morning-run"] [role="alert"]').text())
      .toContain('model provider is unavailable')
    expect(wrapper.get('[data-testid="v2-morning-run"]').text()).not.toContain('CV ready; letter pending')
    wrapper.unmount()
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

  it('keeps the settings save action in its own contained form footer', async () => {
    stubFetch(routes())
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    const footer = wrapper.get('[data-testid="settings-save-footer"]')
    expect(footer.classes()).toContain('settings-card')
    expect(footer.get('button').text()).toMatch(/save settings/i)
  })

  it('translates settings labels and controls into French', async () => {
    document.cookie = 'app_locale=fr; path=/'
    stubFetch(routes())
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    expect(wrapper.text()).toContain('Candidature automatique')
    expect(wrapper.text()).toContain('Planification quotidienne')
    expect(wrapper.find('[aria-label="Score minimum"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Enregistrer les réglages')
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

  it.each([
    ['discover', /run now \(discovery\)/i],
    ['full', /run full pipeline now/i],
  ] as const)('shows a rejected %s launch instead of silently doing nothing', async (kind, label) => {
    stubFetch(routes([{
      match: `/api/runs/${kind}`,
      method: 'POST',
      status: 503,
      json: { detail: 'Run service unavailable' },
    }]))
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()

    await buttonNamed(wrapper, label).trigger('click')
    await flushPromises()

    expect(wrapper.findAll('[role="alert"]').some(alert =>
      alert.text().includes('Run service unavailable'),
    )).toBe(true)
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

  it('shows a V2 queued run as pending, separate from the legacy full pipeline', async () => {
    const http = stubFetch(routes([{ match: '/api/v2/me/morning-runs', method: 'POST', status: 202,
      json: { runs: [{ run_id: '55555555-5555-4555-8555-555555555555', search_profile_id: '33333333-3333-4333-8333-333333333333', status: 'QUEUED' }] } }]))
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    await buttonNamed(wrapper, /run v2 discovery/i).trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="v2-morning-run"]').text()).toContain('Queued')
    expect(wrapper.get('[data-testid="v2-morning-run"]').text()).not.toContain('application submitted')
    expect(http.callsTo('/api/runs/full').filter(call => call.method === 'POST')).toHaveLength(0)
    wrapper.unmount()
  })

  it('shows a partial source failure with a link to its search results', async () => {
    stubFetch(routes([{ match: '/api/v2/me/morning-runs/latest', method: 'GET', json: { runs: [{
      run_id: '55555555-5555-4555-8555-555555555555', search_profile_id: '33333333-3333-4333-8333-333333333333',
      status: 'PARTIAL', seen_count: 4, matched_count: 2, started_at: null, finished_at: null,
      source_health: [{ source_key: 'board_a', status: 'UNAVAILABLE', reason: 'SOURCE_TIMEOUT' }], warning_codes: ['SOURCE_TIMEOUT'],
    }] } }]))
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    expect(wrapper.get('[data-testid="v2-morning-run"]').text()).toContain('Partial')
    expect(wrapper.get('[data-testid="v2-morning-run"]').text()).toContain('board_a')
    expect(wrapper.get('[data-testid="v2-morning-run"]').text()).toContain('timed out')
    expect(wrapper.get('[data-testid="v2-morning-run"] a').attributes('href')).toBe('/map?profile=33333333-3333-4333-8333-333333333333')
    wrapper.unmount()
  })

  it('keeps V2 discovery available when the legacy settings read fails', async () => {
    stubFetch(routes([{ match: '/api/settings', method: 'GET', status: 503, json: { detail: 'legacy unavailable' } }]))
    const wrapper = await mountSuspended(SettingsPage)
    await flushPromises()
    expect(wrapper.get('[data-testid="v2-morning-run"]')).toBeTruthy()
    expect(buttonNamed(wrapper, /run v2 discovery/i).exists()).toBe(true)
    expect(wrapper.text()).toContain('Live Swiss source coverage is unverified')
    wrapper.unmount()
  })
})
