<!--
  The LLM connection form, shared by the "add a connection" card and each row's
  inline edit on /providers.

  A connection is where a task's prompt could go: a provider type, an endpoint, a
  model, and — for a hosted gateway — a credential (docs/LLM_CONNECTIONS.md). This
  form is deliberately shaped by two rules the backend enforces, so a submission is
  coherent before it is sent rather than 422'd back:

  **A CLI provider carries no endpoint, credential or headers.** Claude Code and Codex
  run a local binary and authenticate themselves; the `LLMConnection` model refuses a
  base URL or a key on one (and injecting an `ANTHROPIC_*` key would violate the Phase
  11 security invariant, §1). So when the type is a CLI, those fields are not rendered
  at all — there is no disabled input to mislead, and nothing to submit.

  **The credential is write-only.** No response ever carries a key back — a saved
  connection is only `has_api_key` (§13) — so this form never shows a stored key. On a
  connection that has one, editing offers three explicit choices: keep it (the
  default, nothing sent), replace it (a new value), or remove it. A key typed here goes
  out once and is never read back.

  Provider type is fixed once created: a connection's adapter is its identity, and
  changing it would be a different connection. So the type is a select when creating
  and a static label when editing.
-->
<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import type { ConnectionFormPayload } from '~/composables/useLlmConnections'
import type { LLMConnection, LLMProviderType } from '~/types/v2'
import { errorMessage, fieldErrors } from '~/utils/v2-errors'

const props = defineProps<{
  /** The connection being edited, or null for one being created. */
  connection?: LLMConnection | null
  pending?: boolean
  error?: unknown
  submitLabel?: string
}>()

const emit = defineEmits<{ submit: [payload: ConnectionFormPayload] }>()

/** The closed set, each with whether it speaks HTTP (an endpoint and a credential). */
const PROVIDER_TYPES: { value: LLMProviderType, label: string, api: boolean }[] = [
  { value: 'CLAUDE_CODE', label: 'Claude Code (local CLI, no API key)', api: false },
  { value: 'CODEX', label: 'Codex (local CLI, no API key)', api: false },
  { value: 'OPENAI_COMPATIBLE', label: 'OpenAI-compatible API (hosted gateway)', api: true },
  { value: 'LOCAL_OPENAI_COMPATIBLE', label: 'OpenAI-compatible (local server)', api: true },
]

/** How the credential of a connection that already has one is being changed. */
type KeyMode = 'keep' | 'replace' | 'remove'

const isEditing = computed(() => props.connection != null)

const form = reactive({
  provider_type: 'CLAUDE_CODE' as LLMProviderType,
  display_name: '',
  base_url: '',
  model: '',
  api_key: '',
  custom_headers: '',
  enabled: true,
  is_default: false,
  priority: 100,
})
// On a connection with a stored key, `keep` sends nothing; the user opts into a
// rotate or a clear. On one without, the field just adds a key, so this stays `keep`.
const keyMode = ref<KeyMode>('keep')

// Copied out of the prop rather than bound to it, so no input writes into the cached
// list payload other readers share. Re-runs when the edited connection changes (a row
// opens its editor) and once on mount for the create form's defaults.
watch(() => props.connection, (next) => {
  form.provider_type = next?.provider_type ?? 'CLAUDE_CODE'
  form.display_name = next?.display_name ?? ''
  form.base_url = next?.base_url ?? ''
  form.model = next?.model ?? ''
  form.api_key = ''
  form.custom_headers = headersToText(next?.custom_headers ?? {})
  form.enabled = next?.enabled ?? true
  form.is_default = next?.is_default ?? false
  form.priority = next?.priority ?? 100
  keyMode.value = 'keep'
}, { immediate: true })

const isApi = computed(() =>
  PROVIDER_TYPES.find(entry => entry.value === form.provider_type)?.api ?? false)
const hasStoredKey = computed(() => props.connection?.has_api_key ?? false)

const message = computed(() => (props.error ? errorMessage(props.error) : null))
const fields = computed(() => fieldErrors(props.error))

const canSubmit = computed(() => {
  if (props.pending || form.display_name.trim() === '') return false
  // An API endpoint cannot be reached without a base URL, and the backend requires
  // one; catching it here keeps the "requires a base_url" 422 off a well-meaning save.
  if (isApi.value && form.base_url.trim() === '') return false
  // Replacing a key means providing one; an empty "replace" is a no-op the form should
  // not submit as a rotation.
  if (isApi.value && keyMode.value === 'replace' && form.api_key.trim() === '') return false
  return true
})

/** `CLAUDE_CODE` as its friendly label, for the static line shown while editing. */
const providerLabel = computed(() =>
  PROVIDER_TYPES.find(entry => entry.value === form.provider_type)?.label
  ?? form.provider_type)

function headersToText(headers: Record<string, string>): string {
  return Object.entries(headers).map(([key, value]) => `${key}: ${value}`).join('\n')
}

/**
 * A `Key: value` per line into a header map. A line without a colon is skipped rather
 * than guessed at; the backend forbids a credential in a header, which the hint says.
 */
function textToHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const at = line.indexOf(':')
    if (at === -1) continue
    const key = line.slice(0, at).trim()
    const value = line.slice(at + 1).trim()
    if (key !== '') out[key] = value
  }
  return out
}

/** The credential half of the payload, per the three states the backend accepts. */
function credential(): { api_key: string | null, remove_api_key: boolean } {
  // A CLI carries no key, whatever the field held before the type was switched.
  if (!isApi.value) return { api_key: null, remove_api_key: false }
  if (isEditing.value && hasStoredKey.value) {
    if (keyMode.value === 'remove') return { api_key: null, remove_api_key: true }
    if (keyMode.value === 'replace') return { api_key: form.api_key.trim(), remove_api_key: false }
    return { api_key: null, remove_api_key: false } // keep
  }
  // Creating, or a connection with no stored key: an empty box means no credential.
  const typed = form.api_key.trim()
  return { api_key: typed === '' ? null : typed, remove_api_key: false }
}

function submit() {
  const { api_key, remove_api_key } = credential()
  emit('submit', {
    provider_type: form.provider_type,
    display_name: form.display_name.trim(),
    // A CLI sends no URL, model or headers; the backend refuses them on one.
    base_url: isApi.value ? form.base_url.trim() : null,
    model: form.model.trim() === '' ? null : form.model.trim(),
    api_key,
    remove_api_key,
    custom_headers: isApi.value ? textToHeaders(form.custom_headers) : {},
    enabled: form.enabled,
    is_default: form.is_default,
    priority: form.priority,
  })
}
</script>

<template>
  <form class="acct-form" novalidate @submit.prevent="submit">
    <p v-if="message" class="auth-error" role="alert">
      {{ message }}
    </p>

    <template v-if="isEditing">
      <p class="acct-field">
        Provider
        <span class="acct-kept">{{ providerLabel }}</span>
      </p>
    </template>
    <template v-else>
      <label class="acct-field" for="conn-type">Provider</label>
      <select
        id="conn-type"
        v-model="form.provider_type"
        :disabled="pending"
        aria-label="Provider type"
      >
        <option v-for="entry in PROVIDER_TYPES" :key="entry.value" :value="entry.value">
          {{ entry.label }}
        </option>
      </select>
    </template>

    <label class="acct-field" for="conn-name">Name</label>
    <input
      id="conn-name"
      v-model="form.display_name"
      type="text"
      required
      placeholder="My gateway"
      :disabled="pending"
      aria-label="Display name"
    >
    <p v-if="fields.display_name" class="auth-field-error" role="alert">
      {{ fields.display_name }}
    </p>

    <template v-if="isApi">
      <label class="acct-field" for="conn-url">Endpoint</label>
      <input
        id="conn-url"
        v-model="form.base_url"
        type="url"
        required
        placeholder="https://gateway.example.com/v1"
        :disabled="pending"
        aria-label="Base URL"
      >
      <p class="auth-hint">
        A local server (Ollama, LM Studio) must use a loopback address such as
        <code>http://127.0.0.1:11434/v1</code>.
      </p>
      <p v-if="fields.base_url" class="auth-field-error" role="alert">
        {{ fields.base_url }}
      </p>
    </template>

    <label class="acct-field" for="conn-model">Model <span class="auth-optional">(optional)</span></label>
    <input
      id="conn-model"
      v-model="form.model"
      type="text"
      placeholder="gpt-4o-mini"
      :disabled="pending"
      aria-label="Model"
    >

    <!-- CLI providers manage their own auth: no key field is rendered, so there is
         nothing to submit and nothing to mislead. -->
    <template v-if="isApi">
      <fieldset v-if="isEditing && hasStoredKey" class="acct-fieldset">
        <legend>API key</legend>
        <p class="auth-hint">
          A key is stored. It is never shown; choose what to do with it.
        </p>
        <label class="acct-check">
          <input v-model="keyMode" type="radio" value="keep" :disabled="pending">
          Keep the stored key
        </label>
        <label class="acct-check">
          <input v-model="keyMode" type="radio" value="replace" :disabled="pending">
          Replace it
        </label>
        <label class="acct-check">
          <input v-model="keyMode" type="radio" value="remove" :disabled="pending">
          Remove it
        </label>
        <input
          v-if="keyMode === 'replace'"
          v-model="form.api_key"
          type="password"
          autocomplete="off"
          placeholder="New API key"
          :disabled="pending"
          aria-label="New API key"
        >
      </fieldset>
      <template v-else>
        <label class="acct-field" for="conn-key">
          API key <span class="auth-optional">(optional)</span>
        </label>
        <input
          id="conn-key"
          v-model="form.api_key"
          type="password"
          autocomplete="off"
          placeholder="sk-…"
          :disabled="pending"
          aria-label="API key"
        >
        <p class="auth-hint">
          Stored encrypted and never shown again. Leave blank for a gateway that needs
          none.
        </p>
      </template>

      <label class="acct-field" for="conn-headers">
        Custom headers <span class="auth-optional">(optional)</span>
      </label>
      <textarea
        id="conn-headers"
        v-model="form.custom_headers"
        rows="2"
        :disabled="pending"
        placeholder="X-Org: acme"
        aria-label="Custom headers"
      />
      <p class="auth-hint">
        One <code>Name: value</code> per line — route hints only. Never put an API key
        in a header; use the field above.
      </p>
    </template>

    <label class="acct-field" for="conn-priority">Priority</label>
    <input
      id="conn-priority"
      v-model.number="form.priority"
      type="number"
      min="0"
      :disabled="pending"
      aria-label="Priority"
    >
    <p class="auth-hint">
      Lower runs first when more than one connection could serve a task.
    </p>

    <!-- On create only: enabled and default are their own endpoints once a connection
         exists, so an edit changes them from the row, not here. -->
    <template v-if="!isEditing">
      <label class="acct-check">
        <input v-model="form.enabled" type="checkbox" :disabled="pending">
        Enabled
      </label>
      <label class="acct-check">
        <input v-model="form.is_default" type="checkbox" :disabled="pending">
        Make this the default connection
      </label>
    </template>

    <div class="acct-actions">
      <button class="btn-primary" type="submit" :disabled="!canSubmit">
        {{ pending ? 'Saving…' : (submitLabel ?? 'Save connection') }}
      </button>
    </div>
  </form>
</template>
