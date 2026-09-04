<!--
  Kanban board. Ported from webapp/src/components/Board.tsx.

  Three behaviours are load-bearing and easy to lose in a rewrite:

    * every lifecycle column renders even when empty, so any status is reachable
      by drag — a board that hid empty columns would make some transitions
      impossible;
    * dropping a card on its own column is a no-op, so no spurious status_change
      event is recorded;
    * `dragover` must call `preventDefault()` or the browser refuses the drop.

  The empty-state string is French, as in V1. That is inconsistent with the rest
  of the UI, which is English; it is reproduced rather than fixed because
  translating a user-visible string is a product change.
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { useJobs } from '~/composables/useQueries'
import { useSetJobStatus } from '~/composables/useMutations'
import { STATUS_ORDER, statusColor } from '~/utils/status'
import type { TrackKey } from '~/utils/tracks'

const props = withDefaults(defineProps<{ track?: TrackKey }>(), { track: 'job' })
const emit = defineEmits<{ open: [jobId: number] }>()

const { data, status: fetchStatus } = useJobs({ view: 'board' })
const setStatus = useSetJobStatus()

// Which card is being dragged, and which column is hovered (for the highlight).
const drag = ref<{ jobId: number, status: string } | null>(null)
const overCol = ref<string | null>(null)

const isLoading = computed(() => fetchStatus.value === 'pending')
const board = computed(() => data.value?.board ?? {})

/** Cards for a status column, restricted to the active track (job vs travail). */
function col(status: string) {
  return (board.value[status] ?? []).filter(c => (c.track ?? 'job') === props.track)
}

const total = computed(() => STATUS_ORDER.reduce((n, s) => n + col(s).length, 0))

function onDragOver(event: DragEvent, status: string) {
  if (!drag.value) return
  event.preventDefault() // required to make the column a valid drop target
  if (overCol.value !== status) overCol.value = status
}

function onDragLeave(status: string) {
  if (overCol.value === status) overCol.value = null
}

/** Drop a dragged card onto a status column → persist the new status. */
function drop(status: string) {
  if (drag.value && drag.value.status !== status) {
    setStatus.mutate({ jobId: drag.value.jobId, status })
  }
  drag.value = null
  overCol.value = null
}
</script>

<template>
  <p v-if="isLoading">
    Loading…
  </p>
  <p v-else-if="total === 0" style="color: var(--text-dim)">
    Rien dans ce pipeline pour l'instant.
  </p>
  <div v-else class="board">
    <div
      v-for="status in STATUS_ORDER"
      :key="status"
      class="board-col"
      :class="{ 'drag-over': overCol === status }"
      :data-status="status"
      @dragover="onDragOver($event, status)"
      @dragleave="onDragLeave(status)"
      @drop="drop(status)"
    >
      <div class="board-col-head" :style="{ '--badge-color': statusColor(status) }">
        <h3>{{ status }}</h3>
        <span class="board-count">{{ col(status).length }}</span>
      </div>
      <div
        v-for="c in col(status)"
        :key="c.application_id"
        class="board-card"
        draggable="true"
        @dragstart="drag = { jobId: c.job_id, status }"
        @dragend="drag = null; overCol = null"
        @click="emit('open', c.job_id)"
      >
        <div class="board-card-company">
          {{ c.company }}
        </div>
        <div class="board-card-title">
          {{ c.title }}
        </div>
        <div class="board-card-meta">
          <span class="num" style="color: var(--text-dim)">
            {{ c.phone_screen_pct !== null ? `${c.phone_screen_pct}% screen` : '—' }}
          </span>
          <ScoreChip :score="c.score" />
        </div>
      </div>
    </div>
  </div>
</template>
