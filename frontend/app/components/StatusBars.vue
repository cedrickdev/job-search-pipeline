<!--
  Pipeline-by-status bars. The other block both webapp pages rendered inline and
  identically; the label is a StatusBadge so the row carries the same colour
  coding as the board.
-->
<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ rows: { status: string, count: number }[] }>()

const max = computed(() => Math.max(1, ...props.rows.map(r => r.count)))
</script>

<template>
  <div class="ov-bars">
    <div v-for="s in props.rows" :key="s.status" class="ov-bar-row">
      <span class="ov-bar-label"><StatusBadge :status="s.status" /></span>
      <span class="ov-bar-track">
        <span
          class="ov-bar-fill ov-bar-accent"
          :style="{ width: `${(s.count / max) * 100}%` }"
        />
      </span>
      <span class="ov-bar-val">{{ s.count }}</span>
    </div>
  </div>
</template>
