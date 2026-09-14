<!--
  The list beside the map, and the other half of the one-selection rule (§12).

  Every result is here, including the ones the map cannot show: a remote-only or unresolved
  role has no pin, but it is a real result and it lives in this list with a distance of "—"
  and a "No location" tag (§8, §9). That is the point of listing from the full result set
  rather than from the markers — the list is the complete answer, the map is the placeable
  subset of it.

  A row is a real `<button>`, so it is reachable and operable from the keyboard (§31), and
  `aria-current` marks the selected one for assistive tech. Selecting a row emits the id and
  nothing else; whether that row came from a map click or a keypress, the store is the only
  thing that ends up holding the selection.
-->
<script setup lang="ts">
import { watch, ref } from 'vue'
import type { CompanyGeoItem, OpportunityGeoItem } from '~/types/v2'
import { distanceLabel, remoteLabel, statusLabel } from '~/utils/geo-format'

const props = defineProps<{
  mode: 'opportunities' | 'companies'
  opportunities?: OpportunityGeoItem[]
  companies?: CompanyGeoItem[]
  selectedId?: string | null
}>()
const emit = defineEmits<{ select: [id: string] }>()

const listEl = ref<HTMLUListElement | null>(null)

/** An opportunity is placeable only if the server gave it a point (map-projection.ts). */
function opportunityPlaced(item: OpportunityGeoItem): boolean {
  return Boolean(item.location?.point)
}
function companyPlaced(item: CompanyGeoItem): boolean {
  return Boolean(item.location.point)
}

// Keep the chosen row in view when the selection was made elsewhere — a marker click on
// the map should not leave its list row scrolled off-screen.
watch(() => props.selectedId, (id) => {
  if (!id || !listEl.value) return
  const row = listEl.value.querySelector<HTMLElement>(`[data-id="${id}"]`)
  row?.scrollIntoView({ block: 'nearest' })
})
</script>

<template>
  <ul ref="listEl" class="map-list" :aria-label="mode === 'companies' ? 'Employers' : 'Opportunities'">
    <template v-if="mode === 'opportunities'">
      <li v-for="item in opportunities" :key="item.id" class="map-list-li">
        <button
          type="button"
          class="map-row"
          :class="{ 'map-row--active': item.id === selectedId }"
          :data-id="item.id"
          :aria-current="item.id === selectedId ? 'true' : undefined"
          @click="emit('select', item.id)"
        >
          <span class="map-row-main">
            <span class="map-row-title">{{ item.title }}</span>
            <span class="map-row-sub">{{ item.company_name }}</span>
          </span>
          <span class="map-row-meta">
            <span v-if="!opportunityPlaced(item)" class="map-row-tag">No location</span>
            <span v-else-if="remoteLabel(item.remote_scope)" class="map-row-tag">
              {{ remoteLabel(item.remote_scope) }}
            </span>
            <span class="map-row-dist">{{ distanceLabel(item.distance_meters) }}</span>
          </span>
        </button>
      </li>
    </template>

    <template v-else>
      <li v-for="item in companies" :key="item.company.id" class="map-list-li">
        <button
          type="button"
          class="map-row"
          :class="{ 'map-row--active': item.company.id === selectedId }"
          :data-id="item.company.id"
          :aria-current="item.company.id === selectedId ? 'true' : undefined"
          @click="emit('select', item.company.id)"
        >
          <span class="map-row-main">
            <span class="map-row-title">{{ item.company.name }}</span>
            <span class="map-row-sub">{{ statusLabel(item.status) }}</span>
          </span>
          <span class="map-row-meta">
            <span v-if="!companyPlaced(item)" class="map-row-tag">No location</span>
            <span v-else-if="item.is_headquarters" class="map-row-tag">HQ</span>
            <span class="map-row-dist">{{ distanceLabel(item.distance_meters) }}</span>
          </span>
        </button>
      </li>
    </template>
  </ul>
</template>

<style scoped>
.map-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
  overflow-y: auto;
}
.map-row {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  text-align: left;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  padding: var(--space-2) var(--space-3);
  cursor: pointer;
  color: var(--text);
}
.map-row:hover {
  background: var(--surface-2);
}
.map-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.map-row--active {
  background: var(--accent-tint);
  border-color: var(--accent);
}
.map-row-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.map-row-title {
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.map-row-sub {
  font-size: 12px;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.map-row-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
}
.map-row-tag {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--text-muted);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 1px 6px;
}
.map-row-dist {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-muted);
}
</style>
