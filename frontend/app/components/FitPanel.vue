<!--
  Keyword-coverage summary for the drawer's Fit tab. Ported from
  webapp/src/components/FitPanel.tsx.

  `available: false` means the job has no description on file, so there is
  nothing to compare the CV against — it says so instead of rendering a 0%
  coverage that would read as a bad match rather than as missing input.
-->
<script setup lang="ts">
import type { FitReport } from '~/types/domain'

const props = defineProps<{ fit: FitReport }>()
</script>

<template>
  <p v-if="!props.fit.available" style="color: var(--text-dim)">
    No job description on file — fit analysis unavailable.
  </p>
  <div v-else>
    <p>
      Coverage
      <span class="num">{{ Math.round((props.fit.coverage_score ?? 0) * 100) }}%</span>
      <span class="fit-tier" :class="`tier-${props.fit.risk_tier?.toLowerCase()}`">
        {{ props.fit.risk_tier }}
      </span>
    </p>
    <p v-if="props.fit.missing_keywords && props.fit.missing_keywords.length > 0">
      Missing: {{ props.fit.missing_keywords.join(', ') }}
    </p>
  </div>
</template>
