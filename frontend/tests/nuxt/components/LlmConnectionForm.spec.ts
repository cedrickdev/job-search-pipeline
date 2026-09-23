// The LLM connection form, tested for the two rules it exists to keep before a
// submission reaches the backend, and for the write-only credential.
//
// **A CLI provider carries no endpoint, key or headers; an API one carries them all.**
// The security invariant (§1) is that Claude Code and Codex authenticate themselves —
// the platform never stores or injects a key. So a CLI selection must not merely
// disable those fields but omit them, and a submit for one must send `base_url: null`,
// `api_key: null` and no headers.
//
// **The credential is write-only, with three states on an edit.** A form for a
// connection that stores a key shows keep/replace/remove — never the value — and each
// choice maps to exactly one of the (api_key, remove_api_key) pairs the backend
// accepts. On a create, or a connection with no stored key, an empty box is "no
// credential", not an empty string.
import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import LlmConnectionForm from '~/components/LlmConnectionForm.vue'
import { llmConnection } from '../support/v2-fixtures'
import { ApiError } from '~/utils/api-client'
import type { ConnectionFormPayload } from '~/composables/useLlmConnections'
import type { LLMConnection } from '~/types/v2'
import type { VueWrapper } from '@vue/test-utils'

const SUBMIT = 'button[type="submit"]'

function mountForm(props: {
  connection?: LLMConnection | null
  pending?: boolean
  error?: unknown
  submitLabel?: string
} = {}) {
  return mountSuspended(LlmConnectionForm, { props: { connection: null, ...props } })
}

function submitted(wrapper: VueWrapper): ConnectionFormPayload {
  const events = wrapper.emitted('submit')
  expect(events, 'the form emitted no submit').toBeTruthy()
  return events!.at(-1)![0] as ConnectionFormPayload
}

describe('LlmConnectionForm · shape by provider type', () => {
  it('opens a create as a CLI type with no endpoint, key or headers field', async () => {
    const wrapper = await mountForm()

    expect(wrapper.get<HTMLSelectElement>('select[aria-label="Provider type"]').element.value)
      .toBe('CLAUDE_CODE')
    expect(wrapper.find('input[aria-label="Base URL"]').exists()).toBe(false)
    expect(wrapper.find('input[aria-label="API key"]').exists()).toBe(false)
    expect(wrapper.find('textarea[aria-label="Custom headers"]').exists()).toBe(false)
    // Only the name is missing — a CLI needs no endpoint.
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined()
  })

  it('reveals the endpoint, key and headers fields for an API type', async () => {
    const wrapper = await mountForm()
    await wrapper.get('select[aria-label="Provider type"]').setValue('OPENAI_COMPATIBLE')

    expect(wrapper.find('input[aria-label="Base URL"]').exists()).toBe(true)
    expect(wrapper.find('input[aria-label="API key"]').exists()).toBe(true)
    expect(wrapper.find('textarea[aria-label="Custom headers"]').exists()).toBe(true)
  })

  it('will not submit an API type without an endpoint', async () => {
    const wrapper = await mountForm()
    await wrapper.get('input[aria-label="Display name"]').setValue('Gateway')
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeUndefined()

    await wrapper.get('select[aria-label="Provider type"]').setValue('OPENAI_COMPATIBLE')
    // An API endpoint cannot be reached without a base URL; the backend requires one.
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined()

    await wrapper.get('input[aria-label="Base URL"]').setValue('https://gw.example.invalid/v1')
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeUndefined()
  })
})

describe('LlmConnectionForm · what it sends', () => {
  it('sends a CLI connection with null endpoint, key and empty headers', async () => {
    const wrapper = await mountForm()
    await wrapper.get('input[aria-label="Display name"]').setValue('My Claude')
    await wrapper.get('form').trigger('submit')

    const payload = submitted(wrapper)
    expect(payload).toMatchObject({
      provider_type: 'CLAUDE_CODE',
      display_name: 'My Claude',
      base_url: null,
      api_key: null,
      remove_api_key: false,
      custom_headers: {},
    })
  })

  it('sends an API connection with its endpoint, key and parsed headers', async () => {
    const wrapper = await mountForm()
    await wrapper.get('select[aria-label="Provider type"]').setValue('OPENAI_COMPATIBLE')
    await wrapper.get('input[aria-label="Display name"]').setValue('Gateway')
    await wrapper.get('input[aria-label="Base URL"]').setValue('https://gw.example.invalid/v1')
    await wrapper.get('input[aria-label="API key"]').setValue('sk-not-a-real-key-000000')
    await wrapper.get('textarea[aria-label="Custom headers"]').setValue('X-Org: acme')
    await wrapper.get('form').trigger('submit')

    const payload = submitted(wrapper)
    expect(payload).toMatchObject({
      provider_type: 'OPENAI_COMPATIBLE',
      base_url: 'https://gw.example.invalid/v1',
      api_key: 'sk-not-a-real-key-000000',
      custom_headers: { 'X-Org': 'acme' },
    })
  })

  it('treats an empty API key box as no credential, not an empty string', async () => {
    const wrapper = await mountForm()
    await wrapper.get('select[aria-label="Provider type"]').setValue('OPENAI_COMPATIBLE')
    await wrapper.get('input[aria-label="Display name"]').setValue('Keyless gateway')
    await wrapper.get('input[aria-label="Base URL"]').setValue('http://127.0.0.1:1234/v1')
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).api_key).toBeNull()
  })

  it('sends enabled and default only on a create', async () => {
    const wrapper = await mountForm()
    await wrapper.get('input[aria-label="Display name"]').setValue('My Claude')
    // The two toggles exist on a create.
    const boxes = wrapper.findAll('.acct-check input[type="checkbox"]')
    expect(boxes.length).toBeGreaterThanOrEqual(2)
    await boxes[1]!.setValue(true) // make default
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).is_default).toBe(true)
  })
})

describe('LlmConnectionForm · editing, credential three states', () => {
  const stored = () => llmConnection({
    provider_type: 'OPENAI_COMPATIBLE',
    display_name: 'Gateway',
    base_url: 'https://gw.example.invalid/v1',
    has_api_key: true,
  })

  it('fills from a connection and fixes the provider type as a label', async () => {
    const wrapper = await mountForm({ connection: stored() })

    // Provider type is identity: shown, not editable, once created.
    expect(wrapper.find('select[aria-label="Provider type"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('OpenAI-compatible')
    expect(wrapper.get<HTMLInputElement>('input[aria-label="Display name"]').element.value)
      .toBe('Gateway')
    expect(wrapper.get<HTMLInputElement>('input[aria-label="Base URL"]').element.value)
      .toBe('https://gw.example.invalid/v1')
    // Enabled/default are their own endpoints on an edit — not in the form.
    expect(wrapper.findAll('.acct-check input[type="checkbox"]')).toHaveLength(0)
  })

  it('keeps a stored key by default, sending neither api_key nor remove', async () => {
    const wrapper = await mountForm({ connection: stored() })
    await wrapper.get('form').trigger('submit')

    const payload = submitted(wrapper)
    expect(payload.api_key).toBeNull()
    expect(payload.remove_api_key).toBe(false)
  })

  it('rotates a key when replace is chosen and a value typed', async () => {
    const wrapper = await mountForm({ connection: stored() })
    await wrapper.get('input[type="radio"][value="replace"]').setValue(true)
    await wrapper.get('input[aria-label="New API key"]').setValue('sk-rotated-key-111111')
    await wrapper.get('form').trigger('submit')

    const payload = submitted(wrapper)
    expect(payload.api_key).toBe('sk-rotated-key-111111')
    expect(payload.remove_api_key).toBe(false)
  })

  it('will not submit a replace with an empty new key', async () => {
    const wrapper = await mountForm({ connection: stored() })
    await wrapper.get('input[type="radio"][value="replace"]').setValue(true)
    // The replace box is empty: an empty rotation is a no-op, so submit stays disabled.
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined()
  })

  it('clears a key when remove is chosen', async () => {
    const wrapper = await mountForm({ connection: stored() })
    await wrapper.get('input[type="radio"][value="remove"]').setValue(true)
    await wrapper.get('form').trigger('submit')

    const payload = submitted(wrapper)
    expect(payload.api_key).toBeNull()
    expect(payload.remove_api_key).toBe(true)
  })

  it('offers a plain add-key field on a connection with no stored key', async () => {
    const wrapper = await mountForm({
      connection: llmConnection({ provider_type: 'OPENAI_COMPATIBLE',
        base_url: 'https://gw.example.invalid/v1', has_api_key: false }),
    })

    // No keep/replace/remove radios; just the optional add-key box.
    expect(wrapper.find('input[type="radio"][value="keep"]').exists()).toBe(false)
    expect(wrapper.find('input[aria-label="API key"]').exists()).toBe(true)
  })
})

describe('LlmConnectionForm · refusals and pending state', () => {
  it('shows a refusal as one sentence in an alert', async () => {
    const wrapper = await mountForm({
      error: new ApiError(422, 'invalid', {
        error: 'llm_connection_invalid',
        detail: 'not valid',
        messages: ['a OPENAI_COMPATIBLE connection requires a base_url'],
      }),
    })

    expect(wrapper.get('.auth-error').text())
      .toBe('These connection settings are not valid together.')
  })

  it('locks every control while a save is in flight', async () => {
    const wrapper = await mountForm({ connection: llmConnection(), pending: true })

    expect(wrapper.get(SUBMIT).text()).toBe('Saving…')
    for (const control of wrapper.findAll('input, select, textarea')) {
      expect(control.attributes('disabled')).toBeDefined()
    }
  })
})
