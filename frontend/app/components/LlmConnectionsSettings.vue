<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import type { ConnectionFormPayload } from '~/composables/useLlmConnections'
import { useLlmConnectionActions, useLlmConnectionHealth, useLlmConnectionsQuery } from '~/composables/useLlmConnections'
import { useClaudeTerminal } from '~/composables/useClaudeTerminal'
import type { CreateLLMConnectionRequest, LLMConnection, LLMConnectionHealth, UpdateLLMConnectionRequest } from '~/types/v2'
import { ApiError } from '~/utils/api-client'
import { errorMessage } from '~/utils/v2-errors'

const { t } = useLocale()
const { data, status, error } = useLlmConnectionsQuery()
const { create, update, remove, setEnabled, setDefault } = useLlmConnectionActions()
const health = useLlmConnectionHealth()
const terminal = useClaudeTerminal()
const { session: terminalSession, pending: terminalPending, error: terminalError, create: createTerminal } = terminal
const editingId = ref<string | null>(null)
const confirmingId = ref<string | null>(null)
const probingId = ref<string | null>(null)
const probes = reactive<Record<string, LLMConnectionHealth>>({})
const connections = computed(() => data.value?.connections ?? [])
const loading = computed(() => status.value === 'pending' && !data.value)
const listError = computed(() => error.value ? errorMessage(error.value) : null)
const terminalStateLabel = computed(() => terminalSession.value
  ? t(`providers.terminalState.${terminalSession.value.state}`, terminalSession.value.state)
  : '')
const terminalFailureMessage = computed(() => {
  if (terminalError.value instanceof ApiError && terminalError.value.detail === 'TERMINAL_CONNECTION_CLOSED') {
    return t('providers.terminalFailure.connectionClosed')
  }
  if (terminalError.value instanceof ApiError && terminalError.value.detail === 'TERMINAL_MISCONFIGURED') {
    return t('providers.terminalFailure.misconfigured')
  }
  const code = terminalSession.value?.safe_error_code
  if (code === 'TERMINAL_UNAVAILABLE') return t('providers.terminalFailure.unavailable')
  if (code === 'TERMINAL_CAPACITY_REACHED') return t('providers.terminalFailure.capacity')
  if (code === 'TERMINAL_START_FAILED') return t('providers.terminalFailure.startFailed')
  if (terminalError.value instanceof ApiError && terminalError.value.status >= 500) {
    return t('providers.terminalFailure.unavailable')
  }
  return terminalError.value ? errorMessage(terminalError.value) : null
})

const labels: Record<LLMConnection['provider_type'], string> = {
  CLAUDE_CODE: 'Claude Code', CODEX: 'Codex', OPENAI_COMPATIBLE: 'OpenAI-compatible', LOCAL_OPENAI_COMPATIBLE: 'Local OpenAI-compatible',
}
function providerLabel(value: LLMConnection['provider_type']) { return t(`providers.listType.${value}`, labels[value]) }
function healthLabel(value: LLMConnectionHealth['status']) { return t(`providers.health.${value}`, value) }
function isHealthy(result: LLMConnectionHealth) { return result.status === 'HEALTHY' }

async function onCreate(payload: ConnectionFormPayload) {
  await create.mutateAsync({ provider_type: payload.provider_type, display_name: payload.display_name, base_url: payload.base_url, model: payload.model, api_key: payload.api_key, custom_headers: payload.custom_headers, enabled: payload.enabled, is_default: payload.is_default, priority: payload.priority } satisfies CreateLLMConnectionRequest)
}
async function onUpdate(connectionId: string, payload: ConnectionFormPayload) {
  await update.mutateAsync({ connectionId, changes: { display_name: payload.display_name, base_url: payload.base_url, model: payload.model, api_key: payload.api_key, remove_api_key: payload.remove_api_key, custom_headers: payload.custom_headers, priority: payload.priority } satisfies UpdateLLMConnectionRequest })
  editingId.value = null
}
async function onProbe(connectionId: string) {
  probingId.value = connectionId
  try { probes[connectionId] = await health.mutateAsync(connectionId) }
  catch (caught) { probes[connectionId] = { status: 'MISCONFIGURED', detail: errorMessage(caught), latency_ms: null } }
  finally { probingId.value = null }
}
</script>

<template>
  <section id="llm" class="settings-card llm-settings" aria-labelledby="llm-heading">
    <div class="llm-settings__head">
      <div>
        <h2 id="llm-heading">{{ t('providers.connections') }}</h2>
        <p class="settings-hint">{{ t('providers.intro') }}</p>
      </div>
      <button v-if="terminalSession?.state !== 'ACTIVE'" class="btn-ghost" type="button" :disabled="terminalPending" @click="createTerminal().catch(() => {})">
        {{ terminalPending ? t('common.loading') : t('providers.openClaude') }}
      </button>
    </div>
    <p class="settings-hint">{{ t('providers.openClaudeHint') }}</p>
    <div v-if="terminalSession" class="terminal-status" role="status" aria-live="polite">
      <span :class="terminalSession.state === 'ACTIVE' ? 'settings-ok' : 'settings-err'">{{ terminalStateLabel }}</span>
    </div>
    <p v-if="terminalFailureMessage" class="settings-err" role="alert">{{ terminalFailureMessage }}</p>
    <ClaudeTerminalPanel :terminal="terminal" />

    <p v-if="loading" class="ov-state">{{ t('providers.loadingConnections') }}</p>
    <p v-else-if="listError" class="ov-state ov-error">{{ listError }}</p>
    <template v-else>
      <p v-if="connections.length === 0" class="ov-empty">{{ t('providers.noConnections') }}</p>
      <ul v-else class="acct-searches">
        <li v-for="conn in connections" :key="conn.id" class="acct-search">
          <div class="acct-search-row">
            <div class="acct-search-main">
              <span class="acct-search-name">{{ conn.display_name }} <span v-if="conn.is_default" class="acct-live">· {{ t('providers.default') }}</span></span>
              <span class="acct-search-meta">{{ providerLabel(conn.provider_type) }}<template v-if="conn.model"> · {{ conn.model }}</template><template v-if="conn.has_api_key"> · {{ t('providers.keySet') }}</template></span>
            </div>
            <span :class="conn.enabled ? 'acct-live' : 'acct-paused'">{{ conn.enabled ? t('providers.enabled') : t('providers.disabled') }}</span>
          </div>
          <p v-if="probes[conn.id]" :class="isHealthy(probes[conn.id]!) ? 'settings-ok' : 'settings-err'" role="status">{{ healthLabel(probes[conn.id]!.status) }}<template v-if="probes[conn.id]!.detail"> — {{ probes[conn.id]!.detail }}</template></p>
          <div class="acct-actions">
            <button class="btn-ghost" type="button" :disabled="probingId === conn.id" @click="onProbe(conn.id)">{{ probingId === conn.id ? t('providers.testing') : t('providers.test') }}</button>
            <button v-if="!conn.is_default" class="btn-ghost" type="button" :disabled="setDefault.isPending" @click="setDefault.mutate(conn.id)">{{ t('providers.makeDefault') }}</button>
            <button class="btn-ghost" type="button" :disabled="setEnabled.isPending" @click="setEnabled.mutate({ connectionId: conn.id, enabled: !conn.enabled })">{{ conn.enabled ? t('providers.disable') : t('providers.enable') }}</button>
            <button class="btn-ghost" type="button" @click="editingId = editingId === conn.id ? null : conn.id">{{ editingId === conn.id ? t('providers.cancel') : t('providers.edit') }}</button>
            <template v-if="confirmingId === conn.id">
              <span class="settings-err">{{ t('providers.deleteQuestion') }}</span>
              <button class="btn-ghost" type="button" :disabled="remove.isPending" @click="remove.mutate(conn.id); confirmingId = null">{{ t('providers.confirm') }}</button>
              <button class="btn-ghost" type="button" @click="confirmingId = null">{{ t('providers.keep') }}</button>
            </template>
            <button v-else class="btn-ghost" type="button" @click="confirmingId = conn.id">{{ t('providers.delete') }}</button>
          </div>
          <LlmConnectionForm v-if="editingId === conn.id" :connection="conn" :pending="update.isPending" :error="update.error" :submit-label="t('providers.saveChanges')" @submit="payload => onUpdate(conn.id, payload).catch(() => {})" />
        </li>
      </ul>
      <div class="llm-settings__add">
        <h3>{{ t('providers.addConnection') }}</h3>
        <LlmConnectionForm :connection="null" :pending="create.isPending" :error="create.error" :submit-label="t('providers.addConnection')" @submit="payload => onCreate(payload).catch(() => {})" />
        <p v-if="create.isSuccess" class="settings-ok" role="status">{{ t('providers.connectionAdded') }}</p>
      </div>
    </template>
  </section>
</template>

<style scoped>
.llm-settings { gap: .8rem; }
.llm-settings__head { display:flex; align-items:flex-start; justify-content:space-between; gap:1rem; flex-wrap:wrap; }
.llm-settings__head h2,.llm-settings__add h3 { margin:0; }
.llm-settings__add { border-top:1px solid var(--ui-border, #e5e7eb); padding-top:1rem; display:flex; flex-direction:column; gap:.75rem; }
.terminal-status { display:flex; align-items:center; gap:.75rem; flex-wrap:wrap; }
@media (max-width:620px) { .llm-settings__head > .btn-ghost { width:100%; min-height:44px; } }
</style>
