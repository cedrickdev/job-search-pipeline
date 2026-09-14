<!--
  What the map's colours mean — and, as importantly, what has no pin at all.

  There are no match scores here and nowhere on this screen (§42): the map shows where
  things are, not how good a fit they are. The one editorial line is the last one, which
  tells the reader that remote-only and unresolved roles are real results living in the
  list, not omissions — the counterpart to §8 and §9 on the rendering side.
-->
<script setup lang="ts">
defineProps<{ mode: 'opportunities' | 'companies' }>()
</script>

<template>
  <div class="map-legend">
    <span class="map-legend-item">
      <span class="map-legend-dot map-legend-dot--exact" />
      Located
    </span>
    <span class="map-legend-item">
      <span class="map-legend-dot map-legend-dot--approx" />
      {{ mode === 'companies' ? 'Approximate area' : 'Approximate — company office' }}
    </span>
    <span class="map-legend-item">
      <span class="map-legend-cluster">5</span>
      Cluster
    </span>
    <span v-if="mode === 'opportunities'" class="map-legend-note">
      Remote-only and unresolved roles have no pin — see the list.
    </span>
  </div>
</template>

<style scoped>
.map-legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  font-size: 12px;
  color: var(--text-dim);
  background: color-mix(in srgb, var(--surface) 88%, transparent);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
}
.map-legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.map-legend-dot {
  width: 12px;
  height: 12px;
  border-radius: 999px;
  border: 2px solid #fff;
}
.map-legend-dot--exact { background: var(--accent); }
.map-legend-dot--approx { background: var(--warn, #f0b429); }
.map-legend-cluster {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 20px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--accent);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  border: 2px solid #fff;
}
.map-legend-note {
  flex-basis: 100%;
  color: var(--text-muted);
}
</style>
