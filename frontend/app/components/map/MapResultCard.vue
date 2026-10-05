<!--
  The card for the one selected record — an opportunity or an employer.

  It renders from the full geo item (looked up by id from the query cache), not from the
  slim marker, because a card wants more than a marker carries: the apply link, the remote
  scope, the distance. There are no fabricated match scores (§42); a card states where a
  thing is and how to reach it, nothing about how good a fit it is.

  A company card links to the existing `/companies/{id}` page rather than reinventing an
  employer detail view (§45). An opportunity that is a company fallback says so in plain
  words (§7); a remote one shows its scope and a distance of "—" (§9).
-->
<script setup lang="ts">
import { computed } from 'vue'
import type { CompanyGeoItem, OpportunityGeoItem } from '~/types/v2'
import { distanceLabel, remoteLabel, statusLabel } from '~/utils/geo-format'
import { safeJobUrl } from '~/utils/safe-job-url'

const props = defineProps<{
  opportunity?: OpportunityGeoItem | null
  company?: CompanyGeoItem | null
  discoveryLocale?: 'fr' | 'en'
  autopilotBusy?: boolean
  autopilotReport?: {
    next_action: string
    stop_reason?: string | null
    application?: { id: string, state: string } | null
  } | null
  queueBusy?: boolean
}>()
const emit = defineEmits<{ close: [], 'run-autopilot': [], 'queue-autopilot': [] }>()
const postingUrl = computed(() => safeJobUrl(props.opportunity?.application_url))
const provenance = computed(() => (props.opportunity?.source_records ?? []).map(source => ({
  ...source,
  safeUrl: safeJobUrl(source.source_url),
})))
const french = computed(() => props.discoveryLocale === 'fr')
function displayDate(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleDateString(french.value ? 'fr-CH' : 'en-CH') : ''
}
</script>

<template>
  <article v-if="opportunity" class="map-card" data-testid="map-card">
    <header class="map-card-head">
      <h3 class="map-card-title">
        {{ opportunity.title }}
      </h3>
      <button type="button" class="map-card-close" aria-label="Close" @click="emit('close')">
        ✕
      </button>
    </header>

    <p class="map-card-company">
      <NuxtLink v-if="opportunity.company_id" :to="`/companies/${opportunity.company_id}`">
        {{ opportunity.company_name }}
      </NuxtLink>
      <span v-else>{{ opportunity.company_name }}</span>
    </p>

    <dl class="map-card-facts">
      <div class="map-card-fact">
        <dt>Location</dt>
        <dd>{{ statusLabel(opportunity.status) }}</dd>
      </div>
      <div v-if="remoteLabel(opportunity.remote_scope)" class="map-card-fact">
        <dt>Remote</dt>
        <dd>{{ remoteLabel(opportunity.remote_scope) }}</dd>
      </div>
      <div class="map-card-fact">
        <dt>Distance</dt>
        <dd>{{ distanceLabel(opportunity.distance_meters) }}</dd>
      </div>
    </dl>

    <p v-if="opportunity.status === 'COMPANY_FALLBACK'" class="map-card-note">
      Approximate location — the pin is the company office, not the role's own address.
    </p>

    <div v-if="opportunity.first_seen_at" class="map-card-provenance">
      <p>{{ french ? 'Première découverte' : 'First seen' }} : {{ displayDate(opportunity.first_seen_at) }}</p>
      <p v-if="opportunity.last_seen_at">
        {{ french ? 'Dernière observation' : 'Last seen' }} : {{ displayDate(opportunity.last_seen_at) }}
      </p>
      <p v-if="opportunity.last_changed_at">
        {{ french ? 'Dernière mise à jour' : 'Last updated' }} : {{ displayDate(opportunity.last_changed_at) }}
      </p>
      <p v-if="opportunity.first_seen_at === opportunity.last_seen_at">
        {{ french ? 'Nouvelle offre' : 'New opportunity' }}
      </p>
      <p v-else-if="opportunity.last_changed_at">{{ french ? 'Offre mise à jour' : 'Updated opportunity' }}</p>
      <p v-else>{{ french ? 'Déjà observée' : 'Seen before' }}</p>
    </div>
    <div v-if="provenance.length" class="map-card-provenance">
      <span>{{ french ? 'Sources' : 'Sources' }} :</span>
      <ul>
        <li v-for="source in provenance" :key="`${source.source_key}:${source.source_url ?? ''}`">
          <a v-if="source.safeUrl" :href="source.safeUrl" target="_blank" rel="noopener noreferrer">{{ source.source_key }}</a>
          <span v-else>{{ source.source_key }}</span>
        </li>
      </ul>
    </div>

    <a
      v-if="postingUrl"
      class="btn-primary map-card-apply"
      :href="postingUrl"
      target="_blank"
      rel="noopener noreferrer"
    >
      Open posting ↗
    </a>

    <DocumentGenerateActions :opportunity-id="opportunity.id" />
    <button
      type="button"
      class="btn-primary map-card-autopilot"
      :disabled="props.autopilotBusy"
      @click="emit('run-autopilot')"
    >
      {{ props.autopilotBusy ? (french ? 'Préparation…' : 'Preparing…') : (french ? 'Évaluer et préparer' : 'Evaluate and prepare') }}
    </button>
    <p v-if="props.autopilotReport" class="map-card-autopilot-status" role="status">
      {{ props.autopilotReport.stop_reason ?? props.autopilotReport.next_action }}
    </p>
    <button
      v-if="props.autopilotReport?.application?.state === 'APPROVED'"
      type="button"
      class="btn-primary map-card-autopilot"
      :disabled="props.queueBusy"
      @click="emit('queue-autopilot')"
    >
      {{ props.queueBusy ? (french ? 'Mise en file…' : 'Queueing…') : (french ? 'Lancer Playwright' : 'Queue Playwright') }}
    </button>
  </article>

  <article v-else-if="company" class="map-card" data-testid="map-card">
    <header class="map-card-head">
      <h3 class="map-card-title">
        <NuxtLink :to="`/companies/${company.company.id}`">
          {{ company.company.name }}
        </NuxtLink>
      </h3>
      <button type="button" class="map-card-close" aria-label="Close" @click="emit('close')">
        ✕
      </button>
    </header>

    <dl class="map-card-facts">
      <div class="map-card-fact">
        <dt>Location</dt>
        <dd>{{ statusLabel(company.status) }}</dd>
      </div>
      <div v-if="company.is_headquarters" class="map-card-fact">
        <dt>Site</dt>
        <dd>Headquarters</dd>
      </div>
      <div class="map-card-fact">
        <dt>Distance</dt>
        <dd>{{ distanceLabel(company.distance_meters) }}</dd>
      </div>
    </dl>

    <NuxtLink class="btn-ghost map-card-apply" :to="`/companies/${company.company.id}`">
      View company →
    </NuxtLink>
  </article>
</template>

<style scoped>
.map-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: var(--space-3);
  box-shadow: var(--shadow);
}
.map-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-2);
}
.map-card-title {
  font-size: 15px;
  font-weight: 600;
  margin: 0;
}
.map-card-close {
  background: none;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 2px;
}
.map-card-company {
  font-size: 13px;
  color: var(--text-dim);
  margin: 0;
}
.map-card-facts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  margin: 0;
}
.map-card-fact {
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.map-card-fact dt {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
}
.map-card-fact dd {
  margin: 0;
  font-size: 13px;
  color: var(--text);
}
.map-card-note {
  font-size: 12px;
  color: var(--warn, #f0b429);
  margin: 0;
}
.map-card-apply {
  align-self: flex-start;
  text-decoration: none;
  font-size: 13px;
  margin-top: var(--space-1);
}
.map-card-provenance {
  color: var(--text-dim);
  font-size: 12px;
}
.map-card-provenance p { margin: 0 0 var(--space-1); }
.map-card-provenance ul { margin: var(--space-1) 0 0; padding-left: var(--space-3); }
</style>
