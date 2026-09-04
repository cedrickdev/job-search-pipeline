<!--
  Funnel bars. The block webapp rendered inline and identically in both
  OverviewPage.tsx and AnalyticsPage.tsx.

  Widths are relative to the largest stage, with `max(1, …)` so an all-zero
  funnel divides by 1 instead of producing NaN widths.
-->
<script setup lang="ts">
import { computed } from 'vue'
import type { Maybe } from '~/types/domain'

const props = defineProps<{ rows: { stage: string, count: number, pct: Maybe<number> }[] }>()

const max = computed(() => Math.max(1, ...props.rows.map(r => r.count)))
</script>

<template>
  <div class="ov-bars">
    <div v-for="f in props.rows" :key="f.stage" class="ov-bar-row">
      <span class="ov-bar-label">{{ f.stage }}</span>
      <span class="ov-bar-track">
        <span class="ov-bar-fill" :style="{ width: `${(f.count / max) * 100}%` }" />
      </span>
      <span class="ov-bar-val">
        {{ f.count }}{{ f.pct !== null ? ` · ${Math.round(f.pct * 100)}%` : '' }}
      </span>
    </div>
  </div>
</template>
