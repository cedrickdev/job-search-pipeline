<!--
  Job detail drawer. Ported from webapp/src/components/JobDrawer.tsx.

  Four behaviours carry over deliberately, because each encodes something true
  about the backend rather than a styling choice:

    * every state-changing button routes through ConfirmBar. Approve, Mark
      applied, Skip, Apply now and the status override all set `pending` and wait;
    * the regen chip says "queued" until a *full* run is actually in flight, and
      only then "regenerating now". A full agentic run is the only thing that
      consumes the regen queue, so anything else would be a lie;
    * Apply now appears only for a job that is Ready to apply/Approved, has at
      least one rendered CV, and has no apply already in flight;
    * the status select stays pinned to the current status until the change is
      confirmed, so a cancelled confirmation leaves no stale selection.
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useJobDetail, useRunStatus } from '~/composables/useQueries'
import { useJobActions, useTriggerFull } from '~/composables/useMutations'
import { STATUS_ORDER } from '~/utils/status'

type Tab = 'overview' | 'fit' | 'cv' | 'cover' | 'activity' | 'prep' | 'copilot'

const TABS: Tab[] = ['overview', 'fit', 'cv', 'cover', 'activity', 'prep', 'copilot']

const props = defineProps<{ jobId: number }>()
const emit = defineEmits<{ close: [] }>()

const { data, status: fetchStatus } = useJobDetail(() => props.jobId)
const actions = useJobActions(() => props.jobId)
const runStatus = useRunStatus()
const triggerFull = useTriggerFull()

const tab = ref<Tab>('overview')
const pending = ref<{ label: string, run: () => void } | null>(null)

const isLoading = computed(() => fetchStatus.value === 'pending')

function ask(label: string, run: () => void) {
  pending.value = { label, run }
}

function confirmPending() {
  const current = pending.value
  pending.value = null
  current?.run()
}

const anyPending = computed(
  () =>
    actions.go.isPending
    || actions.applied.isPending
    || actions.skip.isPending
    || actions.setStatus.isPending,
)

// A full agentic run is the only thing that consumes the regen queue, so a live
// full run is what turns "queued" into real "regenerating now" progress.
const fullRunActive = computed(
  () => runStatus.data.value?.state === 'running' && runStatus.data.value?.kind === 'full',
)
// One-click way to actually process a queued/failed regen: kick a full run.
// Disabled while one is already in flight (or the trigger POST is pending).
const runNowDisabled = computed(() => fullRunActive.value || triggerFull.isPending)

const apply = computed(() => data.value?.last_apply ?? null)
const applyActive = computed(
  () => apply.value?.status === 'pending' || apply.value?.status === 'in_progress',
)
const hasCv = computed(() =>
  Boolean(data.value?.cv_versions.en || data.value?.cv_versions.fr),
)
const canApply = computed(
  () =>
    (data.value?.application?.status === 'Ready to apply'
      || data.value?.application?.status === 'Approved')
    && hasCv.value
    && !applyActive.value,
)

function tabLabel(t: Tab): string {
  return t === 'cv' ? 'CV' : t[0]!.toUpperCase() + t.slice(1)
}

/**
 * Status override. Reverts the select to the current status immediately: the
 * change only lands once the confirmation is accepted.
 */
function onStatusChange(event: Event) {
  const select = event.target as HTMLSelectElement
  const next = select.value
  const current = data.value?.application?.status ?? ''
  select.value = current
  if (next && next !== current) {
    ask(`Change status to ${next}`, () => actions.setStatus.mutate(next))
  }
}
</script>

<template>
  <div class="drawer-backdrop" @click="emit('close')" />
  <aside class="drawer" role="dialog" aria-label="Job detail">
    <div v-if="isLoading || !data" class="drawer-body">
      Loading…
    </div>
    <template v-else>
      <div class="drawer-head">
        <h2>{{ data.job.company }}</h2>
        <div class="drawer-sub">
          {{ data.job.title }} · {{ data.job.location ?? '—' }}
        </div>
        <div style="display: flex; gap: 8px; margin-top: 8px; align-items: center">
          <StatusBadge v-if="data.application" :status="data.application.status" />
          <ScoreChip :score="data.score?.score ?? null" />

          <!--
            CV regen lifecycle, most honest state first. A pending request is
            "queued" until a full run actually consumes it, at which point we say
            "regenerating now". Once the request is no longer pending, last_regen
            carries the final outcome — a failure is worth surfacing in the header
            (done is confirmed on the CV tab).
          -->
          <span
            v-if="data.pending_regen"
            class="regen-chip"
            :title="data.pending_regen.notes || undefined"
            :aria-label="fullRunActive ? 'CV regenerating now' : 'CV regeneration queued'"
          >
            {{
              fullRunActive
                ? '↻ Regenerating now…'
                : `⏳ CV regen queued (${data.pending_regen.creativity})`
            }}
          </span>
          <span
            v-else-if="data.last_regen?.status === 'failed'"
            class="regen-chip regen-chip-failed"
            :title="data.last_regen.detail || undefined"
            aria-label="CV regeneration failed"
          >
            ⚠ CV regen failed
          </span>

          <!--
            Manual status override: set any lifecycle status the fixed
            Approve/Mark applied/Skip transitions cannot reach.
          -->
          <select
            aria-label="Change status"
            class="status-select"
            :value="data.application?.status ?? ''"
            @change="onStatusChange"
          >
            <option v-if="!data.application" value="" disabled>
              Set status…
            </option>
            <option v-for="s in STATUS_ORDER" :key="s" :value="s">
              {{ s }}
            </option>
          </select>
        </div>
      </div>

      <div class="drawer-body">
        <template v-if="apply">
          <div v-if="applyActive" class="apply-banner" role="status">
            <strong>Applying now…</strong>
            <span v-if="apply.channel"> ({{ apply.channel }})</span>
            <div class="apply-banner-notes">
              A background worker is submitting this application. This panel updates when it
              finishes.
            </div>
          </div>
          <div
            v-else-if="apply.status === 'applied'"
            class="apply-banner apply-banner-done"
            role="status"
          >
            <strong>✓ Applied</strong>
            <span v-if="apply.detail"> — {{ apply.detail }}</span>
            <div v-if="apply.screenshot_path" class="apply-banner-shot">
              Screenshot: {{ apply.screenshot_path }}
            </div>
          </div>
          <div
            v-else-if="apply.status === 'needs_you'"
            class="apply-banner apply-banner-warn"
            role="status"
          >
            <strong>⚠ Needs you</strong>
            <span v-if="apply.detail"> — {{ apply.detail }}</span>
            <div class="apply-banner-actions">
              <button
                class="btn-primary"
                @click="ask(`Retry apply to ${data.job.company}`, () => actions.applyNow.mutate())"
              >
                Retry
              </button>
            </div>
          </div>
          <div
            v-else-if="apply.status === 'failed'"
            class="apply-banner apply-banner-failed"
            role="status"
          >
            <strong>✕ Failed</strong>
            <span v-if="apply.detail"> — {{ apply.detail }}</span>
            <div class="apply-banner-actions">
              <button
                class="btn-primary"
                @click="ask(`Retry apply to ${data.job.company}`, () => actions.applyNow.mutate())"
              >
                Retry
              </button>
            </div>
          </div>
        </template>

        <div class="drawer-tabs">
          <button
            v-for="t in TABS"
            :key="t"
            class="drawer-tab"
            :class="{ active: tab === t }"
            @click="tab = t"
          >
            {{ tabLabel(t) }}
          </button>
        </div>

        <div v-if="tab === 'overview'">
          <p>{{ data.score?.reasoning ?? 'No score yet.' }}</p>
          <p>
            <a :href="data.job.url" target="_blank" rel="noreferrer">Open original posting ↗</a>
          </p>
          <p style="white-space: pre-wrap; color: var(--text-dim)">
            {{ data.job.description }}
          </p>
        </div>

        <FitPanel v-else-if="tab === 'fit'" :fit="data.fit" />

        <div v-else-if="tab === 'cv'">
          <div v-if="data.pending_regen" class="regen-banner" role="status">
            <strong>
              {{
                fullRunActive
                  ? `CV regeneration in progress (${data.pending_regen.creativity})`
                  : `CV regeneration queued (${data.pending_regen.creativity})`
              }}
            </strong>
            {{
              fullRunActive
                ? '— a run is processing it now; the new CV appears here when it finishes.'
                : "— it'll render on the next run, then appear here automatically."
            }}
            <div v-if="data.pending_regen.notes" class="regen-banner-notes">
              “{{ data.pending_regen.notes }}”
            </div>
            <div class="regen-banner-actions">
              <button
                class="btn-primary regen-run-now"
                :disabled="runNowDisabled"
                @click="triggerFull.mutate()"
              >
                Run pipeline now
              </button>
            </div>
          </div>
          <div
            v-else-if="data.last_regen?.status === 'failed'"
            class="regen-banner regen-banner-failed"
            role="status"
          >
            <strong>CV regeneration failed</strong>
            <div v-if="data.last_regen.detail" class="regen-banner-notes">
              {{ data.last_regen.detail }}
            </div>
            <div class="regen-banner-actions">
              <button
                class="btn-primary regen-run-now"
                :disabled="runNowDisabled"
                @click="triggerFull.mutate()"
              >
                Run pipeline now
              </button>
            </div>
          </div>
          <div
            v-else-if="data.last_regen?.status === 'done'"
            class="regen-banner regen-banner-done"
            role="status"
          >
            <strong>CV regeneration applied</strong>
            <span v-if="data.last_regen.resolved_at">
              {{ data.last_regen.resolved_at.slice(0, 10) }}
            </span>
          </div>

          <template v-for="lang in (['en', 'fr'] as const)" :key="lang">
            <p v-if="data.cv_versions[lang]">
              {{ lang.toUpperCase() }} CV —
              <span class="num">{{ data.cv_versions[lang]!.phone_screen_pct ?? '—' }}%</span>
              <a :href="data.cv_versions[lang]!.pdf_url" target="_blank" rel="noreferrer">
                View PDF ↗
              </a>
            </p>
            <p v-else style="color: var(--text-dim)">
              No {{ lang.toUpperCase() }} CV.
            </p>
          </template>
        </div>

        <div v-else-if="tab === 'cover'">
          <pre v-if="data.cover_letter.en" style="white-space: pre-wrap">{{
            data.cover_letter.en
          }}</pre>
          <p v-else style="color: var(--text-dim)">
            No cover letter drafted.
          </p>
        </div>

        <ul v-else-if="tab === 'activity'" class="drawer-activity">
          <li v-if="data.events.length === 0" style="color: var(--text-dim)">
            No activity yet.
          </li>
          <li v-for="e in data.events" v-else :key="e.id">
            <span class="activity-when">{{ e.created_at.slice(0, 10) }}</span>
            <span class="activity-type">{{ e.event_type }}</span>
            <span v-if="e.detail" class="activity-detail"> — {{ e.detail }}</span>
            <span class="activity-source"> ({{ e.source }})</span>
          </li>
        </ul>

        <PrepTab v-else-if="tab === 'prep'" :job-id="props.jobId" />

        <CopilotPanel v-else-if="tab === 'copilot'" scope="job" :scope-id="props.jobId" />
      </div>

      <ConfirmBar
        v-if="pending"
        :label="pending.label"
        :pending="anyPending"
        @confirm="confirmPending"
        @cancel="pending = null"
      />

      <div class="drawer-actions">
        <button
          class="btn-primary"
          @click="ask(`Approve ${data.job.company}`, () => actions.go.mutate())"
        >
          Approve
        </button>
        <button
          v-if="canApply"
          class="btn-primary"
          @click="ask(`Apply now to ${data.job.company}`, () => actions.applyNow.mutate())"
        >
          Apply now
        </button>
        <button class="btn-ghost" @click="ask('Mark applied', () => actions.applied.mutate())">
          Mark applied
        </button>
        <button class="btn-ghost" @click="ask('Skip', () => actions.skip.mutate())">
          Skip
        </button>
        <button class="btn-ghost" @click="emit('close')">
          Close
        </button>
      </div>
    </template>
  </aside>
</template>
