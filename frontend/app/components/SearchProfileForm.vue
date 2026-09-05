<!--
  The saved-search form, shared by /onboarding and /profile.

  A saved search is what a discovery run reads: `areas` bounds where it looks,
  `queries` and the keyword lists bound what it keeps, and `is_active` decides
  whether it runs at all. This form is deliberately not all of it.

  **What it does not render, it carries.** `PUT /me/search-profiles/{id}` replaces
  the search wholesale, and the draft type requires all eleven fields, so a form
  that sent only its own inputs would reset the rest to their defaults —
  `contract_types`, `posting_languages`, `source_keys` and `workload` would be
  emptied by an edit to a search's name. They are spread from the loaded draft on
  the way out. Same reasoning as ProfileForm.vue, and as settings.vue before it.

  **RADIUS areas are preserved, not edited.** A radius needs a point picked on a
  map, which is the Geo Explorer's job in a later phase; nothing in this build can
  create one. An existing one is listed read-only and sent back unchanged rather
  than dropped, because dropping it would silently narrow somebody's search.

  **An empty list means "no restriction", not "match nothing"** — the domain's
  convention, and the reason the type and mode checkboxes start out all-clear
  instead of all-ticked. `areas` is the one exception: at least one is required,
  because a search with no area is a search of the planet.

  Keyword lists are one entry per line. A comma-separated box would be shorter to
  render and wrong the first time somebody excludes "Legal, Compliance".
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { SearchArea, SearchProfile, SearchProfileDraft } from '~/types/v2'
import { errorMessage, fieldErrors } from '~/utils/v2-errors'

type OpportunityType = SearchProfileDraft['opportunity_types'][number]
type WorkplaceMode = SearchProfileDraft['workplace_modes'][number]

const TYPES: OpportunityType[] = [
  'FULL_TIME', 'PART_TIME', 'STUDENT_JOB', 'INTERNSHIP', 'APPRENTICESHIP',
  'WORK_STUDY', 'GRADUATE', 'TEMPORARY', 'FREELANCE',
]
const MODES: WorkplaceMode[] = ['ON_SITE', 'HYBRID', 'REMOTE']

/** The two area kinds this form can build; `RADIUS` is kept aside untouched. */
type EditableKind = 'COUNTRY' | 'REMOTE_ONLY'
type EditableArea = Extract<SearchArea, { kind: EditableKind }>
interface AreaRow { kind: EditableKind, country: string, label: string | null }

// A predicate, not a bare `filter`: without the type guard the union stays wide
// and `area.country` does not typecheck against the radius member.
function isEditable(area: SearchArea): area is EditableArea {
  return area.kind !== 'RADIUS'
}
const props = defineProps<{
  /** The saved search, or null for one being created. */
  search: SearchProfile | null
  pending?: boolean
  error?: unknown
  submitLabel?: string
}>()

const emit = defineEmits<{ submit: [draft: SearchProfileDraft] }>()

const name = ref('')
const isActive = ref(true)
const areas = ref<AreaRow[]>([])
const keptAreas = ref<SearchArea[]>([])
const queries = ref('')
const titleKeywords = ref('')
const excludedKeywords = ref('')
const types = ref<OpportunityType[]>([])
const modes = ref<WorkplaceMode[]>([])

function toLines(values: readonly string[]): string {
  return values.join('\n')
}

function fromLines(text: string): string[] {
  return text.split('\n').map(line => line.trim()).filter(line => line !== '')
}

/** `FULL_TIME` as "full time"; the enum spelling is for the API, not the reader. */
function readable(value: string): string {
  return value.toLowerCase().split('_').join(' ')
}

// Copied out of the response rather than bound to it, so no `v-model` writes into
// the cached payload other readers share. A new search starts with one empty country
// row: `areas` is the required field, and an empty list is a guaranteed 422.
watch(() => props.search, (next) => {
  const saved = next?.search
  name.value = saved?.name ?? ''
  isActive.value = saved?.is_active ?? true
  const editable = (saved?.areas ?? []).filter(isEditable)
  areas.value = editable.length === 0
    ? [{ kind: 'COUNTRY', country: '', label: null }]
    : editable.map(area => ({
        kind: area.kind,
        country: area.country ?? '',
        label: area.label ?? null,
      }))
  keptAreas.value = (saved?.areas ?? []).filter(area => !isEditable(area))
  queries.value = toLines(saved?.queries ?? [])
  titleKeywords.value = toLines(saved?.title_keywords ?? [])
  excludedKeywords.value = toLines(saved?.excluded_keywords ?? [])
  types.value = [...(saved?.opportunity_types ?? [])]
  modes.value = [...(saved?.workplace_modes ?? [])]
}, { immediate: true })
const message = computed(() => (props.error ? errorMessage(props.error) : null))
const fields = computed(() => fieldErrors(props.error))
const canSubmit = computed(() =>
  name.value.trim() !== ''
  && areas.value.length + keptAreas.value.length > 0
  && !props.pending)

/** The preserved areas, described well enough to know what is being kept. */
const keptLabels = computed(() => keptAreas.value.map((area) => {
  if (area.kind !== 'RADIUS') return 'Area'
  const { latitude, longitude } = area.center
  return area.label ?? `${area.radius_km} km around ${latitude}, ${longitude}`
}))

function addArea() {
  areas.value.push({ kind: 'COUNTRY', country: '', label: null })
}

function removeArea(index: number) {
  areas.value.splice(index, 1)
}

/**
 * One row as the API's discriminated union.
 *
 * Built field by field rather than spread from the loaded area: switching a row
 * from a country to remote-only would otherwise carry the old kind's keys along,
 * and every V2 schema is `extra="forbid"` — the write would be refused over a
 * field the user cannot see. `label` is the one thing worth carrying across.
 */
function areaOf(row: AreaRow): SearchArea {
  const country = row.country.trim().toUpperCase()
  return row.kind === 'COUNTRY'
    ? { kind: 'COUNTRY', country, label: row.label }
    : { kind: 'REMOTE_ONLY', country: country === '' ? null : country, label: row.label }
}

function submit() {
  const saved = props.search?.search
  emit('submit', {
    // The filters with no editor yet, exactly as they were saved.
    contract_types: saved?.contract_types ?? [],
    posting_languages: saved?.posting_languages ?? [],
    source_keys: saved?.source_keys ?? [],
    workload: saved?.workload ?? null,
    name: name.value.trim(),
    is_active: isActive.value,
    // Preserved areas last. `areas` is a set of places to look, so their order
    // carries no meaning and moving them costs nothing.
    areas: [...areas.value.map(areaOf), ...keptAreas.value],
    queries: fromLines(queries.value),
    title_keywords: fromLines(titleKeywords.value),
    excluded_keywords: fromLines(excludedKeywords.value),
    opportunity_types: types.value,
    workplace_modes: modes.value,
  })
}
</script>

<template>
  <form class="acct-form" novalidate @submit.prevent="submit">
    <p v-if="message" class="auth-error" role="alert">
      {{ message }}
    </p>

    <label class="acct-field" for="search-name">Name</label>
    <input
      id="search-name"
      v-model="name"
      type="text"
      required
      placeholder="Backend roles in Switzerland"
      :disabled="pending"
    >
    <p v-if="fields.name" class="auth-field-error" role="alert">
      {{ fields.name }}
    </p>

    <label class="acct-check">
      <input v-model="isActive" type="checkbox" :disabled="pending">
      Run this search
    </label>
    <p class="auth-hint">
      A paused search is kept and skipped by discovery runs.
    </p>

    <fieldset class="acct-fieldset">
      <legend>Where</legend>
      <p class="auth-hint">
        At least one area: a country, or remote-only postings — those optionally
        narrowed to one country.
      </p>
      <div v-for="(row, index) in areas" :key="index" class="acct-row acct-row--list">
        <select
          v-model="row.kind"
          :aria-label="`Area ${index + 1} kind`"
          :disabled="pending"
        >
          <option value="COUNTRY">
            Country
          </option>
          <option value="REMOTE_ONLY">
            Remote only
          </option>
        </select>
        <input
          v-model.trim="row.country"
          type="text"
          maxlength="2"
          placeholder="CH"
          pattern="[A-Za-z]{2}"
          :aria-label="`Area ${index + 1} country`"
          :disabled="pending"
        >
        <button
          class="btn-ghost"
          type="button"
          :aria-label="`Remove area ${index + 1}`"
          :disabled="pending"
          @click="removeArea(index)"
        >
          ✕
        </button>
      </div>
      <p v-for="(text, index) in keptLabels" :key="`kept-${index}`" class="acct-kept">
        {{ text }}
        <span class="auth-optional">(kept as saved — the map view will edit these)</span>
      </p>
      <p v-if="fields.areas || fields.country" class="auth-field-error" role="alert">
        {{ fields.areas ?? fields.country }}
      </p>
      <button class="btn-ghost" type="button" :disabled="pending" @click="addArea">
        + Add an area
      </button>
    </fieldset>

    <label class="acct-field" for="search-queries">Search terms</label>
    <textarea
      id="search-queries"
      v-model="queries"
      rows="3"
      :disabled="pending"
      aria-describedby="search-queries-hint"
    />
    <p id="search-queries-hint" class="auth-hint">
      One per line, as you would type it into a job board. None means the board's
      own default listing for the area.
    </p>
    <p v-if="fields.queries" class="auth-field-error" role="alert">
      {{ fields.queries }}
    </p>

    <div class="acct-row">
      <div>
        <label class="acct-field" for="search-titles">Title must contain</label>
        <textarea id="search-titles" v-model="titleKeywords" rows="3" :disabled="pending" />
      </div>
      <div>
        <label class="acct-field" for="search-excluded">Exclude</label>
        <textarea id="search-excluded" v-model="excludedKeywords" rows="3" :disabled="pending" />
      </div>
    </div>
    <p class="auth-hint">
      One keyword per line. Empty means no restriction.
    </p>

    <fieldset class="acct-fieldset">
      <legend>
        Type of work <span class="auth-optional">(any, if none are ticked)</span>
      </legend>
      <div class="acct-checks">
        <label v-for="type in TYPES" :key="type" class="acct-check">
          <input v-model="types" type="checkbox" :value="type" :disabled="pending">
          {{ readable(type) }}
        </label>
      </div>
    </fieldset>

    <fieldset class="acct-fieldset">
      <legend>
        Workplace <span class="auth-optional">(any, if none are ticked)</span>
      </legend>
      <div class="acct-checks">
        <label v-for="mode in MODES" :key="mode" class="acct-check">
          <input v-model="modes" type="checkbox" :value="mode" :disabled="pending">
          {{ readable(mode) }}
        </label>
      </div>
    </fieldset>

    <div class="acct-actions">
      <button class="btn-primary" type="submit" :disabled="!canSubmit">
        {{ pending ? 'Saving…' : (submitLabel ?? 'Save search') }}
      </button>
    </div>
  </form>
</template>
