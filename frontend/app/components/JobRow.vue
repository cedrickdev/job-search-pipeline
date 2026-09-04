<!--
  One job row in an overview list. Ported from the local `JobRow` of
  webapp/src/routes/OverviewPage.tsx.

  It stays a <button>: the whole row is the click target that opens the job, and
  V1 relied on that for keyboard access.
-->
<script setup lang="ts">
import type { JobCard } from '~/types/domain'

const props = defineProps<{ job: JobCard }>()
defineEmits<{ open: [] }>()
</script>

<template>
  <button type="button" class="job-row" @click="$emit('open')">
    <span class="job-row-main">
      <span class="job-row-company">{{ props.job.company }}</span>
      <span class="job-row-title">{{ props.job.title }}</span>
    </span>
    <span class="job-row-meta">
      <ScoreChip :score="props.job.score" />
      <StatusBadge :status="props.job.status" />
    </span>
  </button>
</template>
