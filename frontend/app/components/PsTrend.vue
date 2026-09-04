<!--
  Phone-screen readiness trend. Ported from the local `PsTrend` of
  webapp/src/routes/AnalyticsPage.tsx.

  Each point is a daily mean (0–100) coloured against the target, so the chart
  answers "was I above the bar that day" without a legend. The series is sparse by
  construction: only days with at least one scored CV produce a column, which is
  why an empty window says "no scored CVs" rather than "no activity".
-->
<script setup lang="ts">
import type { Analytics } from '~/types/domain'

const props = defineProps<{ trend: Analytics['phone_screen_trend'] }>()
</script>

<template>
  <p v-if="props.trend.points.length === 0" class="ov-empty">
    No scored CVs in this window.
  </p>
  <div
    v-else
    class="an-cols an-cols-tall"
    role="img"
    aria-label="phone-screen readiness by day"
  >
    <div
      v-for="p in props.trend.points"
      :key="p.date"
      class="an-col"
      :title="`${p.date}: ${p.value}% (n=${p.n})`"
    >
      <span
        class="an-col-fill"
        :class="p.value >= props.trend.target ? 'an-col-ok' : 'an-col-warn'"
        :style="{ height: `${Math.min(100, p.value)}%` }"
      />
    </div>
  </div>
</template>
