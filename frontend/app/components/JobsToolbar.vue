<!-- Filter bar above the jobs table. Ported from webapp/src/components/Toolbar.tsx. -->
<script setup lang="ts">
import { STATUS_ORDER } from '~/utils/status'

const q = defineModel<string>('q', { required: true })
const status = defineModel<string>('status', { required: true })
const sort = defineModel<string>('sort', { required: true })
</script>

<template>
  <div class="toolbar">
    <input
      v-model="q"
      class="toolbar-input"
      placeholder="Search company or title…"
      aria-label="Search"
    >
    <select v-model="status" aria-label="Status filter">
      <option value="">
        All statuses
      </option>
      <option v-for="s in STATUS_ORDER" :key="s" :value="s">
        {{ s }}
      </option>
    </select>
    <select v-model="sort" aria-label="Sort">
      <option value="recent">
        Most recent
      </option>
      <option value="score">
        Score
      </option>
      <option value="company">
        Company
      </option>
    </select>
  </div>
</template>
