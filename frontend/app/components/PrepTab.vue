<!--
  Interview prep tab. Ported from webapp/src/components/PrepTab.tsx.

  The one subtle piece is where the displayed pack comes from. V1's generate
  mutation wrote the server's response into the query cache instead of refetching
  (`qc.setQueryData`), because a draft that fails the mandate gate is NOT
  persisted server-side — a refetch would return the old prep and lose both the
  draft and its warning. Here the same effect is a local override that shadows the
  query, cleared as soon as a prep mutation succeeds, which is exactly the moment
  V1's invalidation would have replaced the cache entry.
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { PrepData } from '~/types/domain'
import { usePrep } from '~/composables/useQueries'
import { useGeneratePrep, usePrepMutations } from '~/composables/useMutations'

const props = defineProps<{ jobId: number }>()

const { data: fetched, status } = usePrep(() => props.jobId)
const { saveNotes, addInterview } = usePrepMutations(() => props.jobId)
const generate = useGeneratePrep(() => props.jobId)

/** The last generated pack, shadowing the fetched one until a write lands. */
const override = ref<PrepData | null>(null)

const data = computed<PrepData | null>(() => override.value ?? fetched.value ?? null)
const isLoading = computed(() => status.value === 'pending')

// A fresh draft that failed the mandate gate: surfaced (so the user sees it) but
// not saved server-side, hence flagged here rather than treated as ready.
const unverified = computed(() => data.value?.mandate_ok === false)

const notes = ref('')
const round = ref('')
const when = ref('')

watch(data, (next) => {
  if (next) notes.value = next.notes_md
}, { immediate: true })

async function runGenerate() {
  try {
    override.value = await generate.mutateAsync()
  } catch {
    /* the error is already on `generate.error` */
  }
}

function onNotesBlur() {
  if (data.value && notes.value !== data.value.notes_md) {
    override.value = null
    saveNotes.mutate(notes.value)
  }
}

function submitInterview() {
  if (!round.value) return
  override.value = null
  addInterview.mutate({ round_label: round.value, scheduled_for: when.value || undefined })
  round.value = ''
  when.value = ''
}
</script>

<template>
  <p v-if="isLoading || !data">
    Loading prep…
  </p>
  <div v-else>
    <div class="prep-section prep-generate">
      <button class="btn-primary" :disabled="generate.isPending" @click="runGenerate">
        {{ generate.isPending ? 'Generating…' : 'Generate prep' }}
      </button>
      <span v-if="data.generated_at" class="prep-generated-at">
        Generated {{ data.generated_at.slice(0, 16).replace('T', ' ') }}
      </span>
    </div>

    <div v-if="unverified" class="prep-warning" role="alert">
      ⚠ This draft did not clear the safety gate and was not saved.
      <template v-if="data.flags && data.flags.length > 0">
        Flags: {{ data.flags.join(', ') }}.
      </template>
    </div>

    <div class="prep-section">
      <h4>Notes</h4>
      <textarea
        v-model="notes"
        class="prep-notes"
        aria-label="Prep notes"
        @blur="onNotesBlur"
      />
    </div>

    <div v-if="data.likely_questions && data.likely_questions.length > 0" class="prep-section">
      <h4>Likely questions</h4>
      <ul class="prep-list">
        <li v-for="(q, i) in data.likely_questions" :key="i">
          {{ q }}
        </li>
      </ul>
    </div>
    <div v-if="data.talking_points && data.talking_points.length > 0" class="prep-section">
      <h4>Talking points</h4>
      <ul class="prep-list">
        <li v-for="(q, i) in data.talking_points" :key="i">
          {{ q }}
        </li>
      </ul>
    </div>
    <div v-if="data.company_research && data.company_research.length > 0" class="prep-section">
      <h4>Company research</h4>
      <ul class="prep-list">
        <li v-for="(q, i) in data.company_research" :key="i">
          {{ q }}
        </li>
      </ul>
    </div>

    <div class="prep-section">
      <h4>Interview log</h4>
      <ul class="prep-list">
        <li v-for="iv in data.interviews" :key="iv.id" class="iv-row">
          <span>
            {{ iv.round_label }}{{ iv.scheduled_for ? ` · ${iv.scheduled_for.slice(0, 10)}` : '' }}
          </span>
          <span style="color: var(--text-dim)">{{ iv.outcome ?? 'scheduled' }}</span>
        </li>
      </ul>
      <div class="iv-add">
        <input v-model="round" placeholder="Round (e.g. Phone screen)" aria-label="Round label">
        <input v-model="when" type="datetime-local" aria-label="Scheduled for">
        <button class="btn-ghost" :disabled="!round" @click="submitInterview">
          Add
        </button>
      </div>
    </div>
  </div>
</template>
