<!--
  Status pill. Ported from webapp/src/components/StatusBadge.tsx.

  The colour arrives as an inline `--badge-color` custom property, which
  `.status-badge` in assets/css/components.css reads three times (background,
  text, border) via `color-mix`. Keeping it a variable rather than three inline
  declarations is what lets one lookup drive all three.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { statusColor } from '~/utils/status'

const props = defineProps<{ status: string }>()

const style = computed(() => ({ '--badge-color': statusColor(props.status) }))
</script>

<template>
  <span class="status-badge" :style="style">
    <span class="status-dot" />
    {{ status }}
  </span>
</template>
