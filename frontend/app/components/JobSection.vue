<!--
  A titled list of job rows with a count. Ported from the local `JobSection` of
  webapp/src/routes/OverviewPage.tsx.

  The empty copy ("Nothing here right now.") is V1's, and the count renders even
  at zero — an empty "Today" section is information, not an absence.
-->
<script setup lang="ts">
import type { JobCard } from '~/types/domain'

const props = defineProps<{ title: string, jobs: JobCard[] }>()
const emit = defineEmits<{ open: [jobId: number] }>()
</script>

<template>
  <section class="ov-section">
    <h2 class="ov-section-title">
      {{ props.title }} <span class="ov-count">{{ props.jobs.length }}</span>
    </h2>
    <p v-if="props.jobs.length === 0" class="ov-empty">
      Nothing here right now.
    </p>
    <div v-else class="job-list">
      <JobRow
        v-for="j in props.jobs"
        :key="j.application_id"
        :job="j"
        @open="emit('open', j.job_id)"
      />
    </div>
  </section>
</template>
