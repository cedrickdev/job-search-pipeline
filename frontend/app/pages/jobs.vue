<!--
  Jobs — track tabs, board/table toggle, drawer. Ported from
  webapp/src/routes/JobsPage.tsx (React route "/jobs").

  View, track and the open job stay local component state, as V1's `useState` did.
  Hoisting them into Pinia would make them survive navigation, which is arguably
  nicer and definitely not parity, so it is left for a later phase to decide.

  This is the only page that mounts the drawer — V1 mounted it here and nowhere
  else, and the overview navigates here instead of opening its own.
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { TRACKS, type TrackKey } from '~/utils/tracks'

const view = ref<'table' | 'board'>('board')
const track = ref<TrackKey>('job')
const openJob = ref<number | null>(null)

const activeTrack = computed(() => TRACKS.find(t => t.key === track.value) ?? TRACKS[0])
</script>

<template>
  <div>
    <div class="track-tabs" role="tablist" aria-label="Type de recherche">
      <button
        v-for="t in TRACKS"
        :key="t.key"
        role="tab"
        :aria-selected="track === t.key"
        class="track-tab"
        :class="{ active: track === t.key }"
        @click="track = t.key"
      >
        <span class="track-tab-label">{{ t.label }}</span>
        <span class="track-tab-hint">{{ t.hint }}</span>
      </button>
    </div>

    <div
      style="display: flex; justify-content: space-between; align-items: center; margin: 16px 0"
    >
      <h1 style="margin: 0">
        {{ activeTrack.label }}
      </h1>
      <div class="seg">
        <button :class="{ active: view === 'board' }" @click="view = 'board'">
          Board
        </button>
        <button :class="{ active: view === 'table' }" @click="view = 'table'">
          Table
        </button>
      </div>
    </div>

    <JobsBoard v-if="view === 'board'" :track="track" @open="openJob = $event" />
    <JobsTable v-else :track="track" @open="openJob = $event" />

    <JobDrawer v-if="openJob !== null" :job-id="openJob" @close="openJob = null" />
  </div>
</template>
