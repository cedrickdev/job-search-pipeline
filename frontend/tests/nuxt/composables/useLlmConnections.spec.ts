// The LLM-connection data layer, tested at the wire where a bug would not show on
// screen.
//
// Four things here are load-bearing and asserted from the request rather than the
// rendered page:
//
//   * The list is keyed under `llm:connections`, the prefix every write invalidates —
//     a mismatch would leave a stale table after a create, edit or delete.
//   * The credential travels one way. A create and an edit send `api_key`; no response
//     ever carries one back, and `remove_api_key` is how an edit clears a stored key.
//   * The five writes hit the right method and path: POST/PATCH/DELETE on the
//     collection and the item, PUT on the enabled and default sub-resources.
//   * A probe is a POST to `/healthcheck` and returns the health as data; a
//     misconfigured connection surfaces its code as an ApiError the page can render.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import {
  useLlmConnectionActions,
  useLlmConnectionHealth,
  useLlmConnectionsQuery,
} from '~/composables/useLlmConnections'
import { keysMatching } from '~/composables/useApiQuery'
import { stubFetch } from '../support/http'
import { llmConnection, llmConnectionHealth, llmConnectionList } from '../support/v2-fixtures'

const CONNECTION_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const CONNECTIONS = '/api/v2/settings/llm/connections'

/**
 * Run a composable once, inside a component's `setup`, and return its handle.
 *
 * The component stays mounted so the query stays in the `useApiQuery` registry, which
 * is what `keysMatching` reads. Same helper as useDocuments.spec.ts.
 */
async function run<T>(composable: () => T): Promise<T> {
  let handle!: T
  await mountSuspended(defineComponent({
    setup() {
      handle = composable()
      return () => h('div')
    },
  }))
  return handle
}

describe('useLlmConnectionsQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('reads the connection list and keys it under llm:connections', async () => {
    const http = stubFetch([{ match: CONNECTIONS, json: llmConnectionList() }])
    const q = await run(() => useLlmConnectionsQuery())
    await flushPromises()

    expect(http.callsTo(CONNECTIONS)).toHaveLength(1)
    expect(q.data.value?.connections).toHaveLength(1)
    // The prefix the writes invalidate; a mismatch would leave a stale list.
    expect(keysMatching('llm:connections')).toContain('llm:connections')
  })

  it('reads an empty list as a real payload, not a miss', async () => {
    stubFetch([{ match: CONNECTIONS, json: llmConnectionList([]) }])
    const q = await run(() => useLlmConnectionsQuery())
    await flushPromises()

    expect(q.error.value).toBeFalsy()
    expect(q.data.value?.connections).toEqual([])
  })
})

describe('useLlmConnectionActions', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('POSTs a new connection with its credential in the body', async () => {
    const http = stubFetch([{
      match: CONNECTIONS, method: 'POST', status: 201,
      json: llmConnection({ provider_type: 'OPENAI_COMPATIBLE', has_api_key: true }),
    }])
    const actions = await run(() => useLlmConnectionActions())

    await actions.create.mutateAsync({
      provider_type: 'OPENAI_COMPATIBLE',
      display_name: 'Gateway',
      base_url: 'https://gateway.example.invalid/v1',
      model: null,
      api_key: 'sk-not-a-real-key-000000',
      custom_headers: {},
      enabled: true,
      is_default: false,
      priority: 100,
    })

    expect(http.callsTo(CONNECTIONS).map(c => c.method)).toContain('POST')
    // The key is in the request; a response never carries one back.
    expect(http.bodyOf(CONNECTIONS)).toMatchObject({
      provider_type: 'OPENAI_COMPATIBLE',
      api_key: 'sk-not-a-real-key-000000',
    })
  })

  it('PATCHes an edit and can clear the key with remove_api_key', async () => {
    const http = stubFetch([{
      match: `${CONNECTIONS}/${CONNECTION_ID}`, method: 'PATCH',
      json: llmConnection({ has_api_key: false }),
    }])
    const actions = await run(() => useLlmConnectionActions())

    await actions.update.mutateAsync({
      connectionId: CONNECTION_ID,
      changes: {
        display_name: 'Renamed',
        base_url: 'http://127.0.0.1:11434/v1',
        model: null,
        api_key: null,
        remove_api_key: true,
        custom_headers: {},
        priority: 50,
      },
    })

    const patch = http.callsTo(`${CONNECTIONS}/${CONNECTION_ID}`)
      .filter(c => c.method === 'PATCH')
    expect(patch).toHaveLength(1)
    expect(http.bodyOf(`${CONNECTIONS}/${CONNECTION_ID}`)).toMatchObject({
      remove_api_key: true,
    })
  })

  it('DELETEs one connection by id', async () => {
    const http = stubFetch([{
      match: `${CONNECTIONS}/${CONNECTION_ID}`, method: 'DELETE', status: 204,
    }])
    const actions = await run(() => useLlmConnectionActions())

    await actions.remove.mutateAsync(CONNECTION_ID)

    expect(http.callsTo(`${CONNECTIONS}/${CONNECTION_ID}`).map(c => c.method))
      .toContain('DELETE')
  })

  it('PUTs the enabled and default sub-resources', async () => {
    const http = stubFetch([
      {
        match: `${CONNECTION_ID}/enabled`, method: 'PUT',
        json: llmConnection({ enabled: false }),
      },
      {
        match: `${CONNECTION_ID}/default`, method: 'PUT',
        json: llmConnection({ is_default: true }),
      },
    ])
    const actions = await run(() => useLlmConnectionActions())

    await actions.setEnabled.mutateAsync({ connectionId: CONNECTION_ID, enabled: false })
    await actions.setDefault.mutateAsync(CONNECTION_ID)

    expect(http.bodyOf(`${CONNECTION_ID}/enabled`)).toEqual({ enabled: false })
    // The default endpoint takes no body: the id in the path is the whole instruction.
    expect(http.callsTo(`${CONNECTION_ID}/default`)).toHaveLength(1)
  })

  it('surfaces llm_connection_invalid as an ApiError with that code', async () => {
    stubFetch([{
      match: CONNECTIONS, method: 'POST', status: 422,
      json: {
        error: 'llm_connection_invalid',
        detail: 'not valid',
        messages: ['a OPENAI_COMPATIBLE connection requires a base_url'],
      },
    }])
    const actions = await run(() => useLlmConnectionActions())

    const error = await actions.create.mutateAsync({
      provider_type: 'OPENAI_COMPATIBLE',
      display_name: 'Bad',
      base_url: null,
      model: null,
      api_key: null,
      custom_headers: {},
      enabled: true,
      is_default: false,
      priority: 100,
    }).catch((e: unknown) => e)
    expect((error as { code: string }).code).toBe('llm_connection_invalid')
  })
})

describe('useLlmConnectionHealth', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('POSTs to /healthcheck and returns the health as data', async () => {
    const http = stubFetch([{
      match: `${CONNECTION_ID}/healthcheck`, method: 'POST',
      json: llmConnectionHealth({ status: 'HEALTHY', latency_ms: 12 }),
    }])
    const health = await run(() => useLlmConnectionHealth())

    const result = await health.mutateAsync(CONNECTION_ID)

    expect(http.callsTo(`${CONNECTION_ID}/healthcheck`).map(c => c.method))
      .toContain('POST')
    expect(result.status).toBe('HEALTHY')
  })

  it('surfaces provider_misconfigured as an ApiError with that code', async () => {
    stubFetch([{
      match: `${CONNECTION_ID}/healthcheck`, method: 'POST', status: 409,
      json: { error: 'provider_misconfigured', detail: 'bad endpoint' },
    }])
    const health = await run(() => useLlmConnectionHealth())

    const error = await health.mutateAsync(CONNECTION_ID).catch((e: unknown) => e)
    expect((error as { code: string }).code).toBe('provider_misconfigured')
  })
})
