<!--
  The [Opportunities][Companies] switch (§10). A segmented control, not a dropdown, because
  there are exactly two mutually exclusive views and the choice should read at a glance.

  It owns nothing: `mode` is a two-way binding on the store field the map, the list and the
  query all read (stores/mapExplorer.ts). Switching clears the old selection there, because
  an opportunity id means nothing in the companies view.
-->
<script setup lang="ts">
import type { ExplorerMode } from '~/stores/mapExplorer'

const mode = defineModel<ExplorerMode>({ required: true })

const TABS: { value: ExplorerMode, label: string }[] = [
  { value: 'opportunities', label: 'Opportunities' },
  { value: 'companies', label: 'Companies' },
]
</script>

<template>
  <div class="map-modes" role="tablist" aria-label="Map contents">
    <button
      v-for="tab in TABS"
      :key="tab.value"
      type="button"
      role="tab"
      class="map-mode"
      :class="{ 'map-mode--active': mode === tab.value }"
      :aria-selected="mode === tab.value"
      @click="mode = tab.value"
    >
      {{ tab.label }}
    </button>
  </div>
</template>

<style scoped>
.map-modes {
  display: inline-flex;
  padding: 2px;
  gap: 2px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
}
.map-mode {
  border: none;
  background: transparent;
  color: var(--text-dim);
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  padding: var(--space-1) var(--space-3);
  border-radius: 6px;
  cursor: pointer;
}
.map-mode--active {
  background: var(--accent);
  color: #fff;
}
</style>
