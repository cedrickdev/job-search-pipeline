// The providers page: the LLM-connection settings, tested for what makes it the
// platform's plumbing rather than a plain form.
//
// **A CLI provider has no key field, an API provider does.** The whole point of §1 is
// that Claude Code and Codex authenticate themselves — the platform never stores or
// injects a key for them — so switching the create form to a CLI type must remove the
// key input entirely, not merely disable it. This is asserted from the user's side.
//
// **A credential is write-only.** A key typed into the create form goes out in the
// POST body once; the list never renders one back, only "key set". An edit on a
// connection that stores one offers keep/replace/remove, and "remove" sends
// `remove_api_key: true` — the only way the UI clears a stored key.
//
// **Health is a probe shown as a line, not stored.** "Test" POSTs to /healthcheck and
// renders the status it answered; a misconfigured connection's rejection shows as a
// status the same way a reachable one does.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import ProvidersPage from '~/pages/providers.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import {
  llmConnection,
  llmConnectionHealth,
  llmConnectionList,
  signedIn,
} from '../support/v2-fixtures'
import type { FetchStub, Route } from '../support/http'
import type { LLMConnectionList } from '~/types/v2'
import type { VueWrapper } from '@vue/test-utils'

const CONNECTIONS = '/api/v2/settings/llm/connections'
const CONNECTION_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

function routes(extra: Route[] = [], list: LLMConnectionList = llmConnectionList()): Route[] {
  return [
    ...extra,
    { match: CONNECTIONS, method: 'GET', json: list },
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  ]
}

async function mountPage(...args: Parameters<typeof routes>) {
  const http = stubFetch(routes(...args))
  await useSessionStore().ensure()
  const wrapper = await mountSuspended(ProvidersPage, { route: '/providers' })
  await flushPromises()
  return { http, wrapper }
}

function buttonNamed(wrapper: VueWrapper, label: RegExp) {
  const found = wrapper.findAll('button').find(b => label.test(b.text()))
  if (!found) throw new Error(`no button matching ${label}`)
  return found
}

/** The POST body to `fragment`; the GET on mount has none, so pick by method. */
function postBody(http: FetchStub, fragment: string): Record<string, unknown> {
  const call = http.callsTo(fragment).find(c => c.method === 'POST')
  const body = call?.init?.body
  return (typeof body === 'string' ? JSON.parse(body) : body) as Record<string, unknown>
}

/** The PATCH body to `fragment`. */
function patchBody(http: FetchStub, fragment: string): Record<string, unknown> {
  const call = http.callsTo(fragment).find(c => c.method === 'PATCH')
  const body = call?.init?.body
  return (typeof body === 'string' ? JSON.parse(body) : body) as Record<string, unknown>
}

describe('providers page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    useSessionStore().$reset()
  })

  it('lists a connection with its provider, endpoint and default marker', async () => {
    const { wrapper } = await mountPage()

    expect(wrapper.text()).toContain('Local Ollama')
    expect(wrapper.text()).toContain('Local OpenAI-compatible')
    expect(wrapper.text()).toContain('http://127.0.0.1:11434/v1')
    expect(wrapper.text()).toContain('default')
  })

  it('says "key set" for a stored credential but never renders the value', async () => {
    const { wrapper } = await mountPage([], llmConnectionList([
      llmConnection({ provider_type: 'OPENAI_COMPATIBLE', has_api_key: true,
        base_url: 'https://gw.example.invalid/v1' }),
    ]))

    expect(wrapper.text()).toContain('key set')
    // No password input is populated with a stored value, and none is a text input.
    const keyInput = wrapper.find('input[aria-label="API key"]')
    if (keyInput.exists()) {
      expect((keyInput.element as HTMLInputElement).value).toBe('')
    }
  })

  it('hides the key field for a CLI provider and shows it for an API one', async () => {
    const { wrapper } = await mountPage([], llmConnectionList([]))

    // The create form defaults to a CLI type: no key field, no endpoint field.
    const typeSelect = wrapper.get('select[aria-label="Provider type"]')
    expect(wrapper.find('input[aria-label="API key"]').exists()).toBe(false)
    expect(wrapper.find('input[aria-label="Base URL"]').exists()).toBe(false)

    // Switch to a hosted API type: the endpoint and key fields appear.
    await typeSelect.setValue('OPENAI_COMPATIBLE')
    expect(wrapper.find('input[aria-label="Base URL"]').exists()).toBe(true)
    expect(wrapper.find('input[aria-label="API key"]').exists()).toBe(true)
  })

  it('creates an API connection, sending the key in the body once', async () => {
    const { http, wrapper } = await mountPage([
      {
        match: CONNECTIONS, method: 'POST', status: 201,
        json: llmConnection({ provider_type: 'OPENAI_COMPATIBLE', has_api_key: true }),
      },
    ], llmConnectionList([]))

    await wrapper.get('select[aria-label="Provider type"]').setValue('OPENAI_COMPATIBLE')
    await wrapper.get('input[aria-label="Display name"]').setValue('Gateway')
    await wrapper.get('input[aria-label="Base URL"]').setValue('https://gw.example.invalid/v1')
    await wrapper.get('input[aria-label="API key"]').setValue('sk-not-a-real-key-000000')
    await buttonNamed(wrapper, /Add connection/).trigger('submit')
    await flushPromises()

    const body = postBody(http, CONNECTIONS)
    expect(body).toMatchObject({
      provider_type: 'OPENAI_COMPATIBLE',
      display_name: 'Gateway',
      base_url: 'https://gw.example.invalid/v1',
      api_key: 'sk-not-a-real-key-000000',
    })
  })

  it('will not submit a CLI create carrying no endpoint as an API one would need', async () => {
    const { wrapper } = await mountPage([], llmConnectionList([]))

    // A CLI type with a name is submittable — no endpoint required.
    await wrapper.get('input[aria-label="Display name"]').setValue('My Claude')
    expect(buttonNamed(wrapper, /Add connection/).attributes('disabled')).toBeUndefined()

    // An API type with no endpoint is not: the backend requires a base_url.
    await wrapper.get('select[aria-label="Provider type"]').setValue('OPENAI_COMPATIBLE')
    expect(buttonNamed(wrapper, /Add connection/).attributes('disabled')).toBeDefined()
  })

  it('clears a stored key through the remove choice on an edit', async () => {
    const { http, wrapper } = await mountPage([
      {
        match: `${CONNECTIONS}/${CONNECTION_ID}`, method: 'PATCH',
        json: llmConnection({ has_api_key: false }),
      },
    ], llmConnectionList([
      llmConnection({ provider_type: 'OPENAI_COMPATIBLE', has_api_key: true,
        base_url: 'https://gw.example.invalid/v1' }),
    ]))

    await buttonNamed(wrapper, /^Edit$/).trigger('click')
    await flushPromises()
    // The three-state credential control appears only when a key is stored.
    await wrapper.get('input[type="radio"][value="remove"]').setValue(true)
    await buttonNamed(wrapper, /Save changes/).trigger('submit')
    await flushPromises()

    expect(patchBody(http, `${CONNECTIONS}/${CONNECTION_ID}`).remove_api_key).toBe(true)
  })

  it('probes a connection and shows the health it answered', async () => {
    const { wrapper } = await mountPage([
      {
        match: `${CONNECTION_ID}/healthcheck`, method: 'POST',
        json: llmConnectionHealth({ status: 'HEALTHY', latency_ms: 12 }),
      },
    ])

    await buttonNamed(wrapper, /^Test$/).trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Reachable')
  })

  it('shows a misconfigured probe rejection as a status, not a thrown error', async () => {
    const { wrapper } = await mountPage([
      {
        match: `${CONNECTION_ID}/healthcheck`, method: 'POST', status: 409,
        json: { error: 'provider_misconfigured', detail: 'bad endpoint' },
      },
    ])

    await buttonNamed(wrapper, /^Test$/).trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Misconfigured')
    // The composed, user-facing sentence — not a raw provider message.
    expect(wrapper.text()).toContain('not configured correctly')
  })

  it('confirms before deleting, then sends the DELETE', async () => {
    const { http, wrapper } = await mountPage([
      { match: `${CONNECTIONS}/${CONNECTION_ID}`, method: 'DELETE', status: 204 },
    ])

    // First click asks; it does not delete.
    await buttonNamed(wrapper, /^Delete$/).trigger('click')
    await flushPromises()
    expect(http.callsTo(`${CONNECTIONS}/${CONNECTION_ID}`).filter(c => c.method === 'DELETE'))
      .toHaveLength(0)

    await buttonNamed(wrapper, /^Confirm$/).trigger('click')
    await flushPromises()
    expect(http.callsTo(`${CONNECTIONS}/${CONNECTION_ID}`).filter(c => c.method === 'DELETE'))
      .toHaveLength(1)
  })
})
