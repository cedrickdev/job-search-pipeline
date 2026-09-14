<!--
  "Search this area" (§20). The map never re-queries on its own as the user pans — that
  would fight them and hammer the API. Instead a move marks the view dirty and this button
  appears; pressing it is the explicit request to fetch the current bounds.

  It carries no state: the page decides when it is shown (the view moved since the last
  search) and what "search" means (read `bounds`, refetch). Here it is one button.
-->
<script setup lang="ts">
defineProps<{ loading?: boolean }>()
const emit = defineEmits<{ search: [] }>()
</script>

<template>
  <button
    type="button"
    class="map-search-area"
    :disabled="loading"
    @click="emit('search')"
  >
    <span v-if="loading">Searching…</span>
    <span v-else>Search this area</span>
  </button>
</template>

<style scoped>
.map-search-area {
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 999px;
  padding: var(--space-2) var(--space-4);
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  box-shadow: var(--shadow);
}
.map-search-area:disabled {
  opacity: 0.7;
  cursor: default;
}
</style>
