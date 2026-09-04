<!--
  Settings — auto-apply policy, copilot model, schedule, manual runs. Ported from
  webapp/src/routes/SettingsPage.tsx (React route "/settings").

  The form is a local copy of the server's settings, synced when a fetch lands and
  saved wholesale, exactly as V1 did: the PUT replaces the whole document, so
  binding the inputs straight to a refetching query object would let a background
  refresh overwrite a field mid-edit.

  Two details are backend truths rather than styling. The placeholder endpoints
  mirror server/chat.py's `_BACKEND_DEFAULTS`, and the "must be local" note
  describes a server-side check — the copy exists so the rejection is not a
  surprise.
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { LlmBackend, Settings } from '~/types/domain'
import { useRunStatus, useSettingsQuery } from '~/composables/useQueries'
import { useSaveSettings, useTriggerDiscovery, useTriggerFull } from '~/composables/useMutations'
import { ApiError } from '~/utils/api-client'

const CREATIVITY = ['conservative', 'balanced', 'bold'] as const
const CADENCE = ['daily', 'weekdays'] as const

const BACKENDS: { value: LlmBackend, label: string }[] = [
  { value: 'claude_cli', label: 'Local Claude Code (no API key)' },
  { value: 'ollama', label: 'Ollama (local server)' },
  { value: 'lmstudio', label: 'LM Studio (local server)' },
]

const BACKEND_HINTS: Record<string, { url: string, model: string }> = {
  ollama: { url: 'http://localhost:11434/v1', model: 'llama3.2' },
  lmstudio: { url: 'http://localhost:1234/v1', model: 'local-model' },
}

const { data, status: fetchStatus } = useSettingsQuery()
const save = useSaveSettings()
const { data: runStatus } = useRunStatus()
const discover = useTriggerDiscovery()
const full = useTriggerFull()

const form = ref<Settings | null>(null)
// Copied, not referenced: the inputs mutate this object directly.
watch(data, (next) => {
  if (next) form.value = { ...next.settings }
}, { immediate: true })

const isLoading = computed(() => fetchStatus.value === 'pending')
const running = computed(() => runStatus.value?.state === 'running')
const runningLabel = computed(() =>
  runStatus.value?.started_at
    ? `Running… (started ${runStatus.value.started_at.slice(11, 16)})`
    : 'Running…',
)
const isLocalModel = computed(() => form.value !== null && form.value.llm_backend !== 'claude_cli')
const hint = computed(() => (form.value ? BACKEND_HINTS[form.value.llm_backend] : undefined))
// Only an ApiError carries a server `detail`; anything else is a transport
// failure with nothing worth putting next to the button.
const errDetail = computed(() =>
  save.error instanceof ApiError ? String(save.error.detail) : null,
)
</script>

<template>
  <p v-if="isLoading || !form">
    Loading settings…
  </p>
  <div v-else class="settings-page">
    <h1>Settings</h1>

    <p v-if="data?.status === 'invalid'" class="settings-warn" role="alert">
      Your settings file couldn’t be read; defaults are shown. Saving will rewrite it.
    </p>

    <section class="settings-card">
      <h2>Auto-apply</h2>
      <label class="settings-row settings-row--check">
        <input v-model="form.auto_apply" type="checkbox">
        <span>Enable auto-apply for high-scoring roles</span>
      </label>
      <label class="settings-row">
        <span>Minimum score</span>
        <input
          v-model.number="form.auto_apply_min_score"
          type="number"
          :min="0"
          :max="100"
          aria-label="Minimum score"
        >
      </label>
      <label class="settings-row">
        <span>Daily cap</span>
        <input
          v-model.number="form.auto_apply_daily_cap"
          type="number"
          :min="1"
          aria-label="Daily cap"
        >
      </label>
      <label class="settings-row">
        <span>Tailoring creativity</span>
        <select v-model="form.tailor_creativity" aria-label="Tailoring creativity">
          <option v-for="c in CREATIVITY" :key="c" :value="c">
            {{ c }}
          </option>
        </select>
      </label>
    </section>
    <section class="settings-card">
      <h2>Copilot model</h2>
      <p class="settings-hint">
        Choose which model answers in the copilot. Every option runs on this machine —
        no data leaves your computer and no API key is used.
      </p>
      <label class="settings-row">
        <span>Backend</span>
        <select v-model="form.llm_backend" aria-label="Copilot backend">
          <option v-for="b in BACKENDS" :key="b.value" :value="b.value">
            {{ b.label }}
          </option>
        </select>
      </label>

      <template v-if="isLocalModel">
        <label class="settings-row">
          <span>Endpoint</span>
          <input
            v-model="form.llm_base_url"
            type="text"
            :placeholder="hint?.url"
            aria-label="Local endpoint"
          >
        </label>
        <label class="settings-row">
          <span>Model</span>
          <input
            v-model="form.llm_model"
            type="text"
            :placeholder="hint?.model"
            aria-label="Model name"
          >
        </label>
        <p class="settings-hint">
          Leave blank to use the default ({{ hint?.url }} · {{ hint?.model }}). The endpoint
          must be local (localhost or a loopback address).
        </p>
      </template>
    </section>

    <section class="settings-card">
      <h2>Daily run schedule</h2>
      <p class="settings-hint">
        When enabled, the full pipeline runs automatically in the background at the
        chosen local time. Replaces the old system scheduler.
      </p>
      <label class="settings-row settings-row--check">
        <input v-model="form.schedule_enabled" type="checkbox">
        <span>Run the full pipeline automatically on a schedule</span>
      </label>
      <label class="settings-row">
        <span>Time</span>
        <input v-model="form.schedule_time" type="time" aria-label="Schedule time">
      </label>
      <label class="settings-row">
        <span>Cadence</span>
        <select v-model="form.schedule_cadence" aria-label="Schedule cadence">
          <option v-for="c in CADENCE" :key="c" :value="c">
            {{ c }}
          </option>
        </select>
      </label>
    </section>
    <section class="settings-card">
      <h2>Run now</h2>
      <p class="settings-hint">
        Sweep the sources and update the jobs database on demand, or kick off the
        full agentic pipeline immediately.
      </p>
      <div class="settings-actions">
        <button class="btn-primary" :disabled="running" @click="discover.mutate()">
          {{ running ? runningLabel : 'Run now (discovery)' }}
        </button>
        <button class="btn-ghost" :disabled="running" @click="full.mutate()">
          Run full pipeline now
        </button>
      </div>
      <p
        v-if="runStatus?.last_run && runStatus.state === 'idle'"
        :class="runStatus.last_run.ok ? 'settings-ok' : 'settings-err'"
        role="status"
      >
        Last {{ runStatus.last_run.kind }} run:
        {{ runStatus.last_run.ok ? 'ok' : 'failed' }} — {{ runStatus.last_run.summary }}
      </p>
    </section>

    <div class="settings-actions">
      <button class="btn-primary" :disabled="save.isPending" @click="save.mutate(form)">
        {{ save.isPending ? 'Saving…' : 'Save settings' }}
      </button>
      <span v-if="save.isSuccess && !save.isPending" class="settings-ok">Saved.</span>
      <span v-if="errDetail" class="settings-err" role="alert">{{ errDetail }}</span>
    </div>
  </div>
</template>

