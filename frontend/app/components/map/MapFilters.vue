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
import { computed, ref } from 'vue'
import type { MapFilterForm } from '~/types/map'
import type { OpportunityType, RemoteSelection, WorkplaceMode } from '~/types/v2'
import { useLocale } from '~/composables/useLocale'
import { DEFAULT_COUNTRY_OPTIONS } from '~/composables/useCountryPacks'
import type { CountryOption } from '~/composables/useCountryPacks'

withDefaults(defineProps<{
  mode: 'opportunities' | 'companies'
  countryOptions?: readonly CountryOption[]
}>(), {
  countryOptions: () => DEFAULT_COUNTRY_OPTIONS,
})
const form = defineModel<MapFilterForm>({ required: true })
const emit = defineEmits<{ apply: [] }>()
const { localize: l } = useLocale()
const filtersOpen = ref(false)
const ANY_TYPE = '__any_type'
const ANY_WORKPLACE = '__any_workplace'

const opportunityTypeModel = computed({
  get: () => form.value.opportunityType || ANY_TYPE,
  set: (value: string) => {
    form.value.opportunityType = value === ANY_TYPE ? '' : value as OpportunityType
  },
})
const workplaceModel = computed({
  get: () => form.value.workplaceMode || ANY_WORKPLACE,
  set: (value: string) => {
    form.value.workplaceMode = value === ANY_WORKPLACE ? '' : value as WorkplaceMode
  },
})

const TYPES = computed<{ value: OpportunityType | typeof ANY_TYPE, label: string }[]>(() => [
  { value: ANY_TYPE, label: l('Any type', 'Tous les types') },
  { value: 'FULL_TIME', label: l('Full-time', 'Temps plein') },
  { value: 'PART_TIME', label: l('Part-time', 'Temps partiel') },
  { value: 'STUDENT_JOB', label: l('Student job', 'Emploi étudiant') },
  { value: 'INTERNSHIP', label: l('Internship', 'Stage') },
  { value: 'APPRENTICESHIP', label: l('Apprenticeship', 'Apprentissage') },
  { value: 'WORK_STUDY', label: l('Work-study', 'Alternance') },
  { value: 'GRADUATE', label: l('Graduate', 'Jeune diplômé') },
  { value: 'TEMPORARY', label: l('Temporary', 'Temporaire') },
  { value: 'FREELANCE', label: l('Freelance', 'Indépendant') },
])
const WORKPLACES = computed<{ value: WorkplaceMode | typeof ANY_WORKPLACE, label: string }[]>(() => [
  { value: ANY_WORKPLACE, label: l('Any workplace', 'Tout lieu de travail') },
  { value: 'ON_SITE', label: l('On-site', 'Sur site') },
  { value: 'HYBRID', label: l('Hybrid', 'Hybride') },
  { value: 'REMOTE', label: l('Remote', 'À distance') },
])
const REMOTE = computed<{ value: RemoteSelection, label: string }[]>(() => [
  { value: 'exclude', label: l('Exclude remote', 'Exclure le télétravail') },
  { value: 'include', label: l('Include remote', 'Inclure le télétravail') },
  { value: 'only', label: l('Remote only', 'Télétravail uniquement') },
])

function applyFilters() {
  filtersOpen.value = false
  emit('apply')
}
</script>

<template>
  <section class="map-filter-disclosure">
    <button
      type="button"
      class="btn-ghost map-filters-toggle"
      aria-controls="map-filters-panel"
      :aria-expanded="filtersOpen"
      @click="filtersOpen = !filtersOpen"
    >
      <UIcon name="i-lucide-sliders-horizontal" aria-hidden="true" />
      {{ l('Filter', 'Filtrer') }}
    </button>

    <form v-if="filtersOpen" id="map-filters-panel" class="map-filters" @submit.prevent="applyFilters">
      <template v-if="mode === 'opportunities'">
        <label class="map-field">
          {{ l('Type', 'Type') }}
          <USelect v-model="opportunityTypeModel" :items="TYPES" value-key="value" :aria-label="l('Opportunity type', 'Type d’offre')" />
        </label>
        <label class="map-field">
          {{ l('Workplace', 'Lieu de travail') }}
          <USelect v-model="workplaceModel" :items="WORKPLACES" value-key="value" :aria-label="l('Workplace', 'Lieu de travail')" />
        </label>
        <label class="map-field">
          {{ l('Remote', 'Télétravail') }}
          <USelect v-model="form.remote" :items="REMOTE" value-key="value" :aria-label="l('Remote policy', 'Politique de télétravail')" />
        </label>
      </template>

      <label class="map-field">
        {{ l('Country', 'Pays') }}
        <select
          v-model="form.country"
          :aria-label="l('Country', 'Pays')"
        >
          <option value="">{{ l('Any country', 'Tous les pays') }}</option>
          <option v-for="option in countryOptions" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select>
      </label>

      <label class="map-field map-field--check">
        <input v-model="form.radiusEnabled" type="checkbox" :aria-label="l('Limit to radius', 'Limiter au rayon')">
        {{ l('Limit to radius', 'Limiter au rayon') }}
      </label>
      <label v-if="form.radiusEnabled" class="map-field">
        {{ l('Radius (km)', 'Rayon (km)') }}
        <input
          v-model.number="form.radiusKm"
          type="number"
          min="1"
          max="500"
          :aria-label="l('Radius in kilometres', 'Rayon en kilomètres')"
        >
      </label>

      <button type="submit" class="btn-primary">
        {{ l('Apply filters', 'Appliquer les filtres') }}
      </button>
    </form>
  </section>
</template>

<style scoped>
.map-filter-disclosure {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
}
.map-filters-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}
.map-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  width: 100%;
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  gap: var(--space-3);
}
.map-field {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
  color: var(--text-dim);
}
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
@media (max-width: 560px) {
  .map-filter-disclosure { align-items: stretch; }
  .map-filters-toggle { justify-content: center; }
  .map-filters { align-items: stretch; }
  .map-field { width: 100%; }
  .map-field input[type="number"], .map-field select { min-width: 0; min-height: 44px; font-size: 16px; }
  .map-field--check { min-height: 44px; }
}
</style>
