<!--
  Dependency-free column chart for a zero-filled daily count series. Ported from
  the local `DayBars` of webapp/src/routes/AnalyticsPage.tsx.

  Still dependency-free on purpose. ECharts is in the V2 target stack, but
  swapping the chart engine inside a parity migration would change the rendered
  output, and the migration says parity first — the chart libraries belong to the
  phase that adds the surfaces needing them.

  A window with no activity shows the empty line rather than a row of zero-height
  columns, because the latter looks like a rendering failure.
-->
<script setup lang="ts">
import { computed } from 'vue'
import type { DayPoint } from '~/types/domain'

const props = defineProps<{ data: DayPoint[], accent?: boolean }>()

const max = computed(() => Math.max(1, ...props.data.map(d => d.count)))
const total = computed(() => props.data.reduce((sum, d) => sum + d.count, 0))
</script>

<template>
  <p v-if="total === 0" class="ov-empty">
    No activity in this window.
  </p>
  <div v-else class="an-cols" role="img" aria-label="daily counts">
    <div v-for="d in props.data" :key="d.date" class="an-col" :title="`${d.date}: ${d.count}`">
      <span
        class="an-col-fill"
        :class="{ 'an-col-accent': props.accent }"
        :style="{ height: `${(d.count / max) * 100}%` }"
      />
    </div>
  </div>
</template>
