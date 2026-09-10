<!--
  The company directory: every employer the system knows, whether or not it is hiring.

  That last clause is the screen's whole reason to exist. Until Phase 6 an employer
  was a string on a posting, so a company with no open role could not be looked at —
  and the ones worth an unsolicited application are exactly the ones with nothing
  posted. `has_opportunities=false` is therefore a filter here, not an edge case.

  Intentionally plain: a filter row, a list, a pager and one button. No map (Phase 8
  owns that), no scoring, no bulk actions. What it proves is that the API is real —
  that the pass writes, that the filters filter, that a company with zero
  opportunities comes back — and everything it shows comes from the generated types,
  so a backend rename fails `nuxt typecheck` here rather than at runtime.

  The counts after a pass are shown as the backend reported them, `ambiguous`
  included. A claim that matched two employers was recorded and merged into neither,
  and that is the intended outcome rather than an error to hide: silently picking one
  is how a directory ends up with two employers' facts under one name.
-->
<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { COMPANY_PAGE_SIZE, useCompaniesQuery, useRunCompanyDiscovery } from '~/composables/useCompanies'
import type { AtsPlatform, Company, SpontaneousSupport } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

definePageMeta({ middleware: 'auth' })

useHead({ title: 'Companies · Command Center' })

const PLATFORMS: AtsPlatform[] = ['GREENHOUSE', 'LEVER', 'ASHBY']
const SUPPORT: SpontaneousSupport[] = ['SUPPORTED', 'NOT_SUPPORTED', 'UNKNOWN']

/**
 * The filter state, as the form binds it.
 *
 * Empty string rather than null in the selects, because that is what an unselected
 * `<option value="">` gives back; `filters` below turns it into the absence of a
 * parameter, which is what the API means by "do not restrict on this".
 */
const form = reactive({
  text: '',
  country: '',
  ats_platform: '' as AtsPlatform | '',
  spontaneous_support: '' as SpontaneousSupport | '',
  has_opportunities: '' as '' | 'true' | 'false',
})
const offset = ref(0)

const filters = computed(() => ({
  text: form.text.trim() || undefined,
  country: form.country.trim().toUpperCase() || undefined,
  ats_platform: form.ats_platform || null,
  spontaneous_support: form.spontaneous_support || null,
  has_opportunities: form.has_opportunities === '' ? null : form.has_opportunities === 'true',
  limit: COMPANY_PAGE_SIZE,
  offset: offset.value,
}))

const { data, status, error } = useCompaniesQuery(filters)
const run = useRunCompanyDiscovery()

const isLoading = computed(() => status.value === 'pending' && !data.value)
const companies = computed(() => data.value?.companies ?? [])
const total = computed(() => data.value?.total ?? 0)
const shown = computed(() => {
  if (total.value === 0) return 'no companies'
  const first = offset.value + 1
  return `${first}–${offset.value + companies.value.length} of ${total.value}`
})
const hasNext = computed(() => offset.value + companies.value.length < total.value)
const runMessage = computed(() => (run.error ? errorMessage(run.error) : null))

/** Back to the first page: a new filter makes the old offset meaningless. */
function applyFilters() {
  offset.value = 0
}

function page(delta: number) {
  offset.value = Math.max(0, offset.value + delta * COMPANY_PAGE_SIZE)
}

/** Where an employer is, in as few words as the data allows. */
function where(entry: Company): string {
  const place = entry.locations[0]
  if (!place) return entry.country ?? '—'
  return [place.city, place.region, place.country ?? entry.country]
    .filter(part => part !== null && part !== undefined && part !== '')
    .join(', ') || (place.raw ?? '—')
}

/** The spontaneous-application verdict as three words, `UNKNOWN` included. */
function spontaneous(entry: Company): string {
  switch (entry.spontaneous_application?.support) {
    case 'SUPPORTED': return 'spontaneous: yes'
    case 'NOT_SUPPORTED': return 'spontaneous: no'
    default: return 'spontaneous: unknown'
  }
}
</script>

<template>
  <div class="acct-page co-page">
    <header class="acct-head">
      <h1>Companies</h1>
      <button
        type="button"
        class="btn-primary"
        :disabled="run.isPending"
        @click="run.mutate()"
      >
        {{ run.isPending ? 'Discovering…' : 'Run discovery' }}
      </button>
    </header>

    <p class="settings-hint">
      Employers the system knows, including the ones with no open role. Discovery reads
      configured platforms and the postings already stored; it never crawls the web.
    </p>

    <p v-if="runMessage" class="auth-error">
      {{ runMessage }}
    </p>
    <p v-else-if="run.data" class="settings-ok">
      Pass complete — {{ run.data.created }} created, {{ run.data.matched }} matched,
      {{ run.data.ambiguous }} left ambiguous, {{ run.data.links.linked }} postings linked.
    </p>

    <form class="co-filters" @submit.prevent="applyFilters">
      <label class="acct-field">
        Name
        <input v-model="form.text" type="search" aria-label="Name" placeholder="Logitech">
      </label>
      <label class="acct-field">
        Country
        <input
          v-model="form.country"
          type="text"
          aria-label="Country"
          maxlength="2"
          placeholder="CH"
        >
      </label>
      <label class="acct-field">
        Platform
        <select v-model="form.ats_platform" aria-label="Platform">
          <option value="">
            Any
          </option>
          <option v-for="platform in PLATFORMS" :key="platform" :value="platform">
            {{ platform }}
          </option>
        </select>
      </label>
      <label class="acct-field">
        Spontaneous
        <select v-model="form.spontaneous_support" aria-label="Spontaneous">
          <option value="">
            Any
          </option>
          <option v-for="value in SUPPORT" :key="value" :value="value">
            {{ value }}
          </option>
        </select>
      </label>
      <label class="acct-field">
        Open roles
        <select v-model="form.has_opportunities" aria-label="Open roles">
          <option value="">
            Any
          </option>
          <option value="true">
            With open roles
          </option>
          <option value="false">
            Without open roles
          </option>
        </select>
      </label>
      <button type="submit" class="btn-ghost">
        Filter
      </button>
    </form>

    <p v-if="isLoading" class="ov-state">
      Loading companies…
    </p>
    <p v-else-if="error" class="ov-state ov-error">
      Could not load companies.
    </p>
    <template v-else>
      <p class="acct-kept">
        {{ shown }}
      </p>
      <ul class="acct-searches">
        <li v-for="entry in companies" :key="entry.id" class="acct-search">
          <div class="acct-search-row">
            <div class="acct-search-main">
              <NuxtLink :to="`/companies/${entry.id}`" class="acct-search-name">
                {{ entry.name }}
              </NuxtLink>
              <span class="acct-search-meta">{{ where(entry) }}</span>
            </div>
            <span class="ov-count">{{ entry.identity_status }}</span>
            <span v-if="entry.detected_ats" class="ov-count">
              {{ entry.detected_ats.platform }} · {{ entry.detected_ats.status }}
            </span>
            <span class="acct-search-meta">{{ spontaneous(entry) }}</span>
          </div>
        </li>
      </ul>
      <p v-if="!companies.length" class="ov-empty">
        No company matches these filters.
      </p>
      <div class="acct-actions">
        <button type="button" class="btn-ghost" :disabled="offset === 0" @click="page(-1)">
          ← Previous
        </button>
        <button type="button" class="btn-ghost" :disabled="!hasNext" @click="page(1)">
          Next →
        </button>
      </div>
    </template>
  </div>
</template>
