<!--
  One KPI tile. Extracted from the local `KpiTile` that webapp's OverviewPage.tsx
  and AnalyticsPage.tsx each defined identically; here it is a single
  auto-imported component, which is the Nuxt-native way to share it.
-->
<script setup lang="ts">
defineProps<{ label: string, value: string, sub: string }>()
</script>

<template>
  <div class="kpi-tile">
    <div class="kpi-label">
      {{ label }}
    </div>
    <div class="kpi-value">
      {{ value }}
    </div>
    <div class="kpi-sub">
      {{ sub }}
    </div>
  </div>
</template>
