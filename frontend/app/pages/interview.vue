<!--
  Interview practice: one screen for a session that adapts as it goes.

  Two halves, like the chat. The left starts a session and lists this account's past ones
  with a readiness trend; the right is the open session — its current question, a place to
  answer it (typed or spoken), the coaching that answer earned, and the platform's readiness.

  The phase's one rule is visible in what the right half shows and what it refuses to. An
  answer's coaching is dimensions, strengths, improvements and a suggested answer — never a
  hiring probability or a verdict, because the evaluation carries no such field. Readiness is
  its own card, taken from the session's readiness endpoint, which the platform computes from
  the stored grades — so nothing a provider wrote is ever rendered as a forecast. Practice,
  never prediction (docs/INTERVIEW_SIMULATOR.md).
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useInterviewStore } from '~/stores/interview'
import type { Application, ApplicationList, InterviewMode } from '~/types/v2'
import { apiGet } from '~/utils/api-client'
import { V2_ENDPOINTS } from '~/utils/endpoints'
import { recordClip } from '~/utils/recorder'

definePageMeta({ middleware: 'auth' })

useHead({ title: 'Interview practice · Command Center' })

const store = useInterviewStore()
const route = useRoute()

// The six modes, labelled for the picker; the value is the API enum verbatim.
const MODES: { value: InterviewMode, label: string }[] = [
  { value: 'RECRUITER_HR', label: 'Recruiter / HR screen' },
  { value: 'BEHAVIORAL', label: 'Behavioral' },
  { value: 'TECHNICAL', label: 'Technical' },
  { value: 'HIRING_MANAGER', label: 'Hiring manager' },
  { value: 'CASE_STUDY', label: 'Case study' },
  { value: 'FINAL_INTERVIEW', label: 'Final interview' },
]

const mode = ref<InterviewMode>('BEHAVIORAL')
const opportunityId = ref('')
const draft = ref('')
const recording = ref(false)
let stopRecording: (() => void) | null = null

// The opportunities a session can practice for: the account's applications carry them.
// A `?opportunity=` link is one more source of context — it preselects an opportunity that
// may not have an application yet, so it is added to the list when it is not already there.
const applications = ref<Application[]>([])

const opportunities = computed<{ id: string, label: string }[]>(() => {
  const options = applications.value
    .filter((a): a is Application & { opportunity_id: string } => a.opportunity_id !== null)
    .map(a => ({ id: a.opportunity_id, label: `${a.state} · ${a.opportunity_id.slice(0, 8)}` }))
  const linked = route.query.opportunity
  if (typeof linked === 'string' && !options.some(o => o.id === linked)) {
    options.unshift({ id: linked, label: `Linked · ${linked.slice(0, 8)}` })
  }
  return options
})

async function loadApplications(): Promise<void> {
  try {
    const list = await apiGet<ApplicationList>(V2_ENDPOINTS.applications)
    applications.value = list.applications
  }
  catch {
    // The picker just stays empty; a session can still be opened from a `?opportunity=` link.
  }
}

// Load the account's grounding and its sessions, preselect an opportunity, and open the most
// recent session if there is one — a first visit lands on the empty state and starts one.
onMounted(async () => {
  await Promise.all([
    store.loadProfile(), store.loadSessions(), store.loadHistory(), loadApplications()])
  const linked = route.query.opportunity
  opportunityId.value = typeof linked === 'string' ? linked : opportunities.value[0]?.id ?? ''
  const first = store.sessions[0]
  if (first) await store.select(first.id)
})

// Readiness on the 0-100 scale the platform reports, or a dash when unknown. `coverage` is
// on the unit interval and shown as a percent of the plan the session actually exercised.
const overallLabel = computed(() => {
  const percent = store.readiness?.overall_percent
  return percent === null || percent === undefined ? '—' : `${percent}%`
})
const coverageLabel = computed(() => {
  const coverage = store.readiness?.coverage
  return coverage === undefined ? '—' : `${Math.round(coverage * 100)}%`
})

/** A 0-1 dimension score as a percent, or a dash when that axis has no grade yet. */
function scoreLabel(score: number | null): string {
  return score === null ? '—' : `${Math.round(score * 100)}%`
}

async function startSession(): Promise<void> {
  if (opportunityId.value === '' || store.busy) return
  await store.create(opportunityId.value, mode.value)
}

async function submitAnswer(): Promise<void> {
  const text = draft.value.trim()
  if (text === '' || store.busy) return
  await store.submitText(text)
  if (store.error === null) draft.value = ''
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void submitAnswer()
  }
}

// Push-to-talk: the first click records, the second stops and submits the clip. A denied
// microphone or a browser without MediaRecorder degrades to typing, silently — the composer
// is right there. Mirrors MicButton, but the clip goes to the session, not to /transcribe.
async function toggleVoice(): Promise<void> {
  if (recording.value) {
    stopRecording?.()
    return
  }
  if (store.busy) return
  recording.value = true
  const stop = new Promise<void>((resolve) => {
    stopRecording = resolve
  })
  try {
    const clip = await recordClip(stop)
    await store.submitVoice(clip)
  }
  catch {
    // Voice unavailable or denied — the user types instead.
  }
  finally {
    recording.value = false
    stopRecording = null
  }
}
</script>

<template>
  <section class="sim">
    <aside class="sim__side">
      <div class="sim__side-head">
        <h1>Interview practice</h1>
      </div>

      <form class="sim__new" @submit.prevent="startSession">
        <label class="sim__field">
          <span>Mode</span>
          <select v-model="mode" class="sim__select" aria-label="Interview mode">
            <option v-for="m in MODES" :key="m.value" :value="m.value">{{ m.label }}</option>
          </select>
        </label>
        <label class="sim__field">
          <span>Opportunity</span>
          <select
            v-model="opportunityId"
            class="sim__select"
            aria-label="Opportunity to practice for"
            :disabled="opportunities.length === 0"
          >
            <option
              v-for="o in opportunities"
              :key="o.id"
              :value="o.id"
            >{{ o.label }}</option>
          </select>
        </label>
        <p v-if="opportunities.length === 0" class="sim__muted">
          No applications yet — open one from an opportunity first, or follow an
          <NuxtLink to="/applications">application</NuxtLink> here.
        </p>
        <UButton
          type="submit"
          size="sm"
          :loading="store.busy"
          :disabled="store.busy || opportunityId === ''"
        >
          Start a session
        </UButton>
      </form>

      <!-- SIDE-MARKER -->
      <div class="sim__sessions">
        <h2>Sessions</h2>
        <p v-if="store.sessions.length === 0" class="sim__muted">No sessions yet.</p>
        <ul v-else class="sim__list">
          <li v-for="s in store.sessions" :key="s.id">
            <button
              type="button"
              class="sim__item"
              :class="{ 'sim__item--active': s.id === store.activeId }"
              @click="() => store.select(s.id)"
            >
              <span class="sim__item-title">{{ s.title }}</span>
              <span class="sim__badge" :data-status="s.status">{{ s.status }}</span>
            </button>
          </li>
        </ul>
      </div>

      <div v-if="store.history.length > 0" class="sim__history">
        <h2>Readiness trend</h2>
        <ul class="sim__list">
          <li v-for="h in store.history" :key="h.id" class="sim__trend">
            <span class="sim__trend-band" :data-band="h.readiness.band">
              {{ h.readiness.band }}
            </span>
            <span class="sim__trend-score">
              {{ h.readiness.overall_percent === null ? '—' : `${h.readiness.overall_percent}%` }}
            </span>
            <span class="sim__trend-head">{{ h.headline }}</span>
          </li>
        </ul>
      </div>

    </aside>

    <!-- PANEL-MARKER -->
    <div class="sim__panel">
      <p v-if="store.error" class="sim__error" role="alert">{{ store.error }}</p>

      <div v-if="store.activeId === null" class="sim__empty">
        Start a session to practice for a role. Each answer earns coaching — clarity,
        structure, specificity — and the platform tracks your readiness. It is practice,
        never a prediction of any hiring decision.
      </div>

      <template v-else-if="store.session">
        <header class="sim__head">
          <div>
            <h2>{{ store.session?.title }}</h2>
            <p class="sim__sub">
              {{ store.session?.mode }} · {{ store.session?.difficulty }} · {{ store.session?.style }}
            </p>
          </div>
          <span class="sim__badge" :data-status="store.session?.status">
            {{ store.session?.status }}
          </span>
        </header>

        <section v-if="store.readiness" class="sim__readiness" aria-label="Readiness">
          <div class="sim__readiness-top">
            <span class="sim__band" :data-band="store.readiness?.band">
              {{ store.readiness?.band }}
            </span>
            <span class="sim__overall">{{ overallLabel }}</span>
          </div>
          <p class="sim__muted">
            Coverage {{ coverageLabel }} · {{ store.readiness?.evaluated_answers }} of
            {{ store.readiness?.answered_questions }} answers graded
          </p>
          <ul class="sim__dims">
            <li v-for="d in store.readiness?.dimensions ?? []" :key="d.dimension">
              <span>{{ d.dimension }}</span>
              <span>{{ scoreLabel(d.mean_score) }}</span>
            </li>
          </ul>
          <p class="sim__note">
            A coaching signal the platform computes — not a hiring forecast.
          </p>
        </section>


        <!-- LOOP-MARKER -->
        <section
          v-if="store.awaitingAnswer"
          class="sim__question"
          aria-label="Current question"
        >
          <p class="sim__q-meta">
            Question {{ (store.question?.sequence ?? 0) + 1 }} · {{ store.question?.question_type }}
            <span v-if="store.question?.is_follow_up"> · follow-up</span>
          </p>
          <p class="sim__prompt">{{ store.question?.prompt }}</p>
          <form class="sim__compose" @submit.prevent="submitAnswer">
            <textarea
              v-model="draft"
              class="sim__input"
              aria-label="Your answer"
              placeholder="Type your answer — or record it."
              :disabled="store.busy"
              @keydown="onKeydown"
            />
            <div class="sim__compose-actions">
              <button
                type="button"
                class="sim__mic"
                :aria-label="recording ? 'Stop recording' : 'Record answer'"
                :data-state="recording ? 'recording' : 'idle'"
                :disabled="store.busy && !recording"
                @click="toggleVoice"
              >
                {{ recording ? '⏺ Stop' : '🎤 Record' }}
              </button>
              <UButton
                type="submit"
                :loading="store.busy"
                :disabled="store.busy || !draft.trim()"
              >
                Submit answer
              </UButton>
            </div>
          </form>
        </section>

        <section v-else-if="store.canAsk" class="sim__ask">
          <UButton :loading="store.busy" :disabled="store.busy" @click="() => store.nextQuestion()">
            {{ store.session?.status === 'CREATED' ? 'Ask the first question' : 'Ask the next question' }}
          </UButton>
        </section>

        <!-- COACHING-MARKER -->
        <section v-if="store.lastAnswer" class="sim__answered" aria-label="Last answer">
          <p class="sim__a-meta">
            Your answer · {{ store.lastAnswer?.format }}
            <span v-if="store.lastAnswer?.transcript_confidence != null">
              · transcript {{ Math.round((store.lastAnswer?.transcript_confidence ?? 0) * 100) }}%
            </span>
          </p>
          <p class="sim__answer-text">{{ store.lastAnswer?.content }}</p>

          <div v-if="store.lastEvaluation" class="sim__coaching">
            <h3>Coaching</h3>
            <ul class="sim__dims">
              <li v-for="d in store.lastEvaluation?.dimensions ?? []" :key="d.dimension">
                <span>{{ d.dimension }}</span>
                <span>{{ scoreLabel(d.score) }}</span>
              </li>
            </ul>
            <div v-if="(store.lastEvaluation?.strengths?.length ?? 0) > 0" class="sim__coach-block">
              <h4>Strengths</h4>
              <ul>
                <li v-for="(s, i) in store.lastEvaluation?.strengths ?? []" :key="i">{{ s }}</li>
              </ul>
            </div>
            <div v-if="(store.lastEvaluation?.improvements?.length ?? 0) > 0" class="sim__coach-block">
              <h4>To improve</h4>
              <ul>
                <li v-for="(s, i) in store.lastEvaluation?.improvements ?? []" :key="i">{{ s }}</li>
              </ul>
            </div>
            <div v-if="store.lastEvaluation?.suggested_answer" class="sim__coach-block">
              <h4>A stronger answer</h4>
              <p class="sim__suggested">{{ store.lastEvaluation?.suggested_answer }}</p>
            </div>
          </div>
          <div v-else class="sim__coach-missing">
            <p class="sim__muted">Coaching wasn't produced for this answer.</p>
            <UButton
              size="xs"
              :loading="store.busy"
              :disabled="store.busy"
              @click="() => store.evaluate()"
            >
              Get coaching
            </UButton>
          </div>
        </section>

        <!-- CONTROLS-MARKER -->
        <footer v-if="store.isActive" class="sim__controls">
          <UButton
            v-if="store.session?.status === 'IN_PROGRESS'"
            :loading="store.busy"
            :disabled="store.busy"
            @click="() => store.complete()"
          >
            Complete session
          </UButton>
          <UButton
            variant="ghost"
            :loading="store.busy"
            :disabled="store.busy"
            @click="() => store.abandon()"
          >
            Abandon
          </UButton>
        </footer>

        <section
          v-if="store.isComplete && store.summary"
          class="sim__summary"
          aria-label="Session summary"
        >
          <h3>{{ store.summary?.headline }}</h3>
          <p class="sim__muted">
            {{ store.summary?.questions_asked }} questions ·
            {{ store.summary?.answers_evaluated }} graded
          </p>
          <div v-if="(store.summary?.strengths?.length ?? 0) > 0" class="sim__coach-block">
            <h4>Strengths</h4>
            <ul>
              <li v-for="(s, i) in store.summary?.strengths ?? []" :key="i">{{ s }}</li>
            </ul>
          </div>
          <div v-if="(store.summary?.focus_areas?.length ?? 0) > 0" class="sim__coach-block">
            <h4>Focus next</h4>
            <ul>
              <li v-for="(s, i) in store.summary?.focus_areas ?? []" :key="i">{{ s }}</li>
            </ul>
          </div>
        </section>



      </template>
    </div>

  </section>
</template>

<style scoped>
.sim { display: grid; grid-template-columns: 18rem 1fr; gap: 1rem; height: calc(100vh - 8rem); min-height: 24rem; }
.sim__side { display: flex; flex-direction: column; gap: 1rem; border-right: 1px solid var(--ui-border, #e5e7eb); padding-right: 1rem; overflow-y: auto; }
.sim__side-head h1 { font-size: 1.1rem; font-weight: 600; margin: 0; }
.sim__new { display: flex; flex-direction: column; gap: 0.6rem; }
.sim__field { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.8rem; font-weight: 600; color: var(--ui-text-muted, #4b5563); }
.sim__select { padding: 0.35rem 0.5rem; border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.375rem; font: inherit; background: var(--ui-bg, #fff); }
.sim__muted { font-size: 0.8rem; color: var(--ui-text-muted, #6b7280); margin: 0; }
.sim__sessions, .sim__history { display: flex; flex-direction: column; gap: 0.4rem; }
.sim__sessions h2, .sim__history h2 { font-size: 0.85rem; font-weight: 600; margin: 0; color: var(--ui-text-muted, #4b5563); }
.sim__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.25rem; }
.sim__item { width: 100%; display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; padding: 0.4rem 0.6rem; border-radius: 0.375rem; background: transparent; border: none; cursor: pointer; font: inherit; color: inherit; text-align: left; }
.sim__item:hover { background: var(--ui-bg-elevated, #f3f4f6); }
.sim__item--active { background: var(--ui-bg-elevated, #eef2ff); font-weight: 600; }
.sim__item-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sim__badge { font-size: 0.65rem; font-weight: 700; letter-spacing: 0.02em; padding: 0.1rem 0.4rem; border-radius: 999px; background: var(--ui-bg-elevated, #e5e7eb); color: var(--ui-text-muted, #374151); white-space: nowrap; }
.sim__badge[data-status="IN_PROGRESS"] { background: #eff6ff; color: #1d4ed8; }
.sim__badge[data-status="COMPLETED"] { background: #ecfdf5; color: #047857; }
.sim__badge[data-status="ABANDONED"] { background: #f3f4f6; color: #6b7280; }
.sim__trend { display: grid; grid-template-columns: auto auto 1fr; gap: 0.4rem; align-items: baseline; font-size: 0.8rem; }
.sim__trend-band { font-weight: 700; }
.sim__trend-head { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ui-text-muted, #6b7280); }
.sim__panel { display: flex; flex-direction: column; gap: 0.85rem; min-height: 0; overflow-y: auto; padding-right: 0.25rem; }
.sim__error { color: var(--ui-error, #dc2626); font-size: 0.9rem; margin: 0; }
.sim__empty { color: var(--ui-text-muted, #6b7280); max-width: 34rem; }
.sim__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; }
.sim__head h2 { font-size: 1.15rem; font-weight: 600; margin: 0; }
.sim__sub { font-size: 0.8rem; color: var(--ui-text-muted, #6b7280); margin: 0.15rem 0 0; }
.sim__readiness { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.75rem; display: flex; flex-direction: column; gap: 0.4rem; }
.sim__readiness-top { display: flex; align-items: baseline; gap: 0.6rem; }
.sim__band { font-weight: 700; }
.sim__band[data-band="POLISHED"] { color: #047857; }
.sim__band[data-band="PROGRESSING"] { color: #1d4ed8; }
.sim__band[data-band="DEVELOPING"] { color: #b45309; }
.sim__band[data-band="EARLY"] { color: #9333ea; }
.sim__band[data-band="UNKNOWN"] { color: #6b7280; }
.sim__overall { font-size: 1.4rem; font-weight: 700; }
.sim__dims { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.15rem; }
.sim__dims li { display: flex; justify-content: space-between; font-size: 0.8rem; }
.sim__note { font-size: 0.72rem; color: var(--ui-text-muted, #9ca3af); font-style: italic; margin: 0; }
.sim__question { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.75rem; display: flex; flex-direction: column; gap: 0.5rem; }
.sim__q-meta { font-size: 0.75rem; color: var(--ui-text-muted, #6b7280); margin: 0; }
.sim__prompt { font-size: 1rem; font-weight: 500; margin: 0; }
.sim__compose { display: flex; flex-direction: column; gap: 0.5rem; }
.sim__input { resize: vertical; min-height: 5rem; padding: 0.5rem 0.75rem; border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; font: inherit; }
.sim__compose-actions { display: flex; gap: 0.5rem; align-items: center; }
.sim__mic { padding: 0.35rem 0.6rem; border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.375rem; background: var(--ui-bg, #fff); cursor: pointer; font: inherit; }
.sim__mic[data-state="recording"] { background: #fef2f2; color: #b91c1c; border-color: #fecaca; }
.sim__ask { display: flex; }
.sim__answered { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.75rem; display: flex; flex-direction: column; gap: 0.5rem; }
.sim__a-meta { font-size: 0.75rem; color: var(--ui-text-muted, #6b7280); margin: 0; }
.sim__answer-text { margin: 0; white-space: pre-wrap; word-break: break-word; }
.sim__coaching { display: flex; flex-direction: column; gap: 0.5rem; border-top: 1px solid var(--ui-border, #f3f4f6); padding-top: 0.5rem; }
.sim__coaching h3 { font-size: 0.9rem; font-weight: 600; margin: 0; }
.sim__coach-block h4 { font-size: 0.8rem; font-weight: 600; margin: 0 0 0.15rem; }
.sim__coach-block ul { margin: 0; padding-left: 1.1rem; font-size: 0.85rem; }
.sim__suggested { margin: 0; font-size: 0.85rem; white-space: pre-wrap; word-break: break-word; }
.sim__coach-missing { display: flex; flex-direction: column; gap: 0.4rem; align-items: flex-start; }
.sim__controls { display: flex; gap: 0.5rem; }
.sim__summary { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.75rem; display: flex; flex-direction: column; gap: 0.4rem; background: var(--ui-bg-elevated, #f9fafb); }
.sim__summary h3 { font-size: 1rem; font-weight: 600; margin: 0; }
</style>

