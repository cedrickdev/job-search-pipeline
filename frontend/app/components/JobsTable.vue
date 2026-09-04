<!--
  Table view of the jobs list. Ported from webapp/src/components/JobsTable.tsx.

  The three filters go to the server (`q`, `status`, `sort` are query params, and
  each combination is its own cache key) while the track is filtered client-side
  from the same response — that split is V1's, and it matters: the board and the
  table share the jobs cache, so moving the track filter server-side would split
  it in two.

  `track ?? 'job'` is the V1 default for rows the backend left untracked.
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useJobs } from '~/composables/useQueries'
import type { TrackKey } from '~/utils/tracks'

const props = withDefaults(defineProps<{ track?: TrackKey }>(), { track: 'job' })
const emit = defineEmits<{ open: [jobId: number] }>()

const q = ref('')
const status = ref('')
const sort = ref('recent')

const { data, status: fetchStatus } = useJobs(
  computed(() => ({ q: q.value, status: status.value, sort: sort.value })),
)

const isLoading = computed(() => fetchStatus.value === 'pending')
const items = computed(() =>
  (data.value?.items ?? []).filter(j => (j.track ?? 'job') === props.track),
)
</script>

<template>
  <div>
    <JobsToolbar v-model:q="q" v-model:status="status" v-model:sort="sort" />

    <p v-if="isLoading">
      Loading…
    </p>
    <p v-else-if="items.length === 0" style="color: var(--text-dim)">
      No jobs match these filters.
    </p>
    <table v-else class="jobs-table">
      <thead>
        <tr>
          <th>Company</th>
          <th>Role</th>
          <th>Status</th>
          <th>Score</th>
          <th>Screen %</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="j in items" :key="j.application_id" @click="emit('open', j.job_id)">
          <td class="cell-company">
            {{ j.company }}
          </td>
          <td class="cell-title">
            {{ j.title }}
          </td>
          <td><StatusBadge :status="j.status" /></td>
          <td><ScoreChip :score="j.score" /></td>
          <td class="num">
            {{ j.phone_screen_pct ?? '—' }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
