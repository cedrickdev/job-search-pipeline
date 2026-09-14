<!--
  The filter form. Every control here maps to a parameter the geo API already takes (§23) —
  nothing filters client-side, because PostGIS is the one that decides what is inside a
  radius, a country, or a remote policy (§34). The form is a two-way binding; the page turns
  it into query params (useGeoExplorer.ts) and refetches when "Apply" commits it.

  Which controls show depends on the mode: type, workplace and remote policy are properties
  of a role, so they appear for opportunities only. A radius is offered in both, and it is
  deliberately not a pair of coordinate boxes — the user toggles it on and it is centred on
  the current view (the page reads the map centre), which is the only radius a map gesture
  can honestly express.
-->
<script setup lang="ts">
import type { MapFilterForm } from '~/types/map'
import type { OpportunityType, RemoteSelection, WorkplaceMode } from '~/types/v2'

defineProps<{ mode: 'opportunities' | 'companies' }>()
const form = defineModel<MapFilterForm>({ required: true })
const emit = defineEmits<{ apply: [] }>()

const TYPES: { value: OpportunityType, label: string }[] = [
  { value: 'FULL_TIME', label: 'Full-time' },
  { value: 'PART_TIME', label: 'Part-time' },
  { value: 'STUDENT_JOB', label: 'Student job' },
  { value: 'INTERNSHIP', label: 'Internship' },
  { value: 'APPRENTICESHIP', label: 'Apprenticeship' },
  { value: 'WORK_STUDY', label: 'Work-study' },
  { value: 'GRADUATE', label: 'Graduate' },
  { value: 'TEMPORARY', label: 'Temporary' },
  { value: 'FREELANCE', label: 'Freelance' },
]
const WORKPLACES: { value: WorkplaceMode, label: string }[] = [
  { value: 'ON_SITE', label: 'On-site' },
  { value: 'HYBRID', label: 'Hybrid' },
  { value: 'REMOTE', label: 'Remote' },
]
const REMOTE: { value: RemoteSelection, label: string }[] = [
  { value: 'exclude', label: 'Exclude remote' },
  { value: 'include', label: 'Include remote' },
  { value: 'only', label: 'Remote only' },
]
</script>

<template>
  <form class="map-filters" @submit.prevent="emit('apply')">
    <template v-if="mode === 'opportunities'">
      <label class="map-field">
        Type
        <select v-model="form.opportunityType" aria-label="Opportunity type">
          <option value="">
            Any
          </option>
          <option v-for="type in TYPES" :key="type.value" :value="type.value">
            {{ type.label }}
          </option>
        </select>
      </label>
      <label class="map-field">
        Workplace
        <select v-model="form.workplaceMode" aria-label="Workplace">
          <option value="">
            Any
          </option>
          <option v-for="workplace in WORKPLACES" :key="workplace.value" :value="workplace.value">
            {{ workplace.label }}
          </option>
        </select>
      </label>
      <label class="map-field">
        Remote
        <select v-model="form.remote" aria-label="Remote policy">
          <option v-for="option in REMOTE" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select>
      </label>
    </template>

    <label class="map-field">
      Country
      <input
        v-model="form.country"
        type="text"
        aria-label="Country"
        maxlength="2"
        placeholder="CH"
      >
    </label>

    <label class="map-field map-field--check">
      <input v-model="form.radiusEnabled" type="checkbox" aria-label="Limit to radius">
      Limit to radius
    </label>
    <label v-if="form.radiusEnabled" class="map-field">
      Radius (km)
      <input
        v-model.number="form.radiusKm"
        type="number"
        min="1"
        max="500"
        aria-label="Radius in kilometres"
      >
    </label>

    <button type="submit" class="btn-ghost">
      Apply
    </button>
  </form>
</template>

<style scoped>
.map-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-3);
}
.map-field {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
  color: var(--text-dim);
}
.map-field select,
.map-field input[type="text"],
.map-field input[type="number"] {
  min-width: 120px;
}
.map-field--check {
  flex-direction: row;
  align-items: center;
  gap: var(--space-2);
  align-self: center;
}
.map-field--check input {
  min-width: 0;
}
</style>
