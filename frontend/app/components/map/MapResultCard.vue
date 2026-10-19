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
  autopilotError?: string | null
  queueBusy?: boolean
}>()
const emit = defineEmits<{ close: [], 'run-autopilot': [], 'queue-autopilot': [] }>()
const postingUrl = computed(() => safeJobUrl(props.opportunity?.application_url))
const provenance = computed(() => (props.opportunity?.source_records ?? []).map(source => ({
  ...source,
  safeUrl: safeJobUrl(source.source_url),
})))
const french = computed(() => props.discoveryLocale === 'fr')
const text = (en: string, fr: string) => french.value ? fr : en
const currentLocale = computed<'en' | 'fr'>(() => french.value ? 'fr' : 'en')
function autopilotStatus(report: NonNullable<typeof props.autopilotReport>): string {
  if (report.stop_reason === 'HUMAN_REQUIRED') {
    return text(
      'Preparation is complete. This application needs your review before anything else can happen.',
      'La préparation est terminée. Cette candidature doit être vérifiée par vous avant toute autre étape.',
    )
  }
  if (report.stop_reason === 'DOCUMENT_NOT_USABLE') {
    return text(
      'The documents need your review before the application can continue.',
      'Les documents doivent être vérifiés avant de poursuivre la candidature.',
    )
  }
  if (report.stop_reason === 'DECISION_NOT_ACTIONABLE') {
    return text(
      'This opportunity is not eligible for preparation under your current policy.',
      'Cette offre ne peut pas être préparée avec votre politique actuelle.',
    )
  }
  if (report.next_action === 'QUEUE_BROWSER_SUBMISSION') {
    return text(
      'Everything is ready for your review before browser submission is queued.',
      'Tout est prêt pour votre vérification avant la mise en file de l’envoi navigateur.',
    )
  }
  return report.next_action
}
</script>

<template>
  <article v-if="opportunity" class="map-card" data-testid="map-card">
    <header class="map-card-head">
      <h3 class="map-card-title">
        {{ opportunity.title }}
      </h3>
      <button type="button" class="map-card-close" :aria-label="text('Close', 'Fermer')" @click="emit('close')">
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
        <dt>{{ text('Location', 'Lieu') }}</dt>
        <dd>{{ opportunity.location?.raw ?? statusLabel(opportunity.status, currentLocale) }}</dd>
      </div>
      <div v-if="remoteLabel(opportunity.remote_scope)" class="map-card-fact">
        <dt>{{ text('Remote', 'Télétravail') }}</dt>
        <dd>{{ remoteLabel(opportunity.remote_scope, currentLocale) }}</dd>
      </div>
      <div class="map-card-fact">
        <dt>{{ text('Distance', 'Distance') }}</dt>
        <dd>{{ distanceLabel(opportunity.distance_meters) }}</dd>
      </div>
    </dl>

    <p v-if="opportunity.status === 'COMPANY_FALLBACK'" class="map-card-note">
      {{ text('Approximate location — the pin is the company office, not the role\'s own address.', 'Emplacement approximatif : le repère indique le bureau de l’entreprise, pas l’adresse du poste.') }}
    </p>

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
      {{ text('Open posting ↗', 'Ouvrir l’offre ↗') }}
    </a>

    <DocumentGenerateActions :opportunity-id="opportunity.id" :locale="currentLocale" />
    <button
      type="button"
      class="btn-primary map-card-autopilot"
      :disabled="props.autopilotBusy || props.autopilotReport?.application?.state === 'REQUIRES_HUMAN'"
      @click="emit('run-autopilot')"
    >
      {{ props.autopilotBusy ? (french ? 'Préparation…' : 'Preparing…') : (french ? 'Évaluer et préparer' : 'Evaluate and prepare') }}
    </button>
    <p v-if="props.autopilotReport" class="map-card-autopilot-status" role="status">
      {{ autopilotStatus(props.autopilotReport) }}
    </p>
    <p v-if="props.autopilotError" class="map-card-autopilot-error" role="alert">
      {{ props.autopilotError }}
    </p>
    <NuxtLink
      v-if="props.autopilotReport?.application?.state === 'REQUIRES_HUMAN'"
      class="btn-ghost map-card-apply"
      to="/applications"
    >
      {{ text('Review application →', 'Vérifier la candidature →') }}
    </NuxtLink>
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
      <button type="button" class="map-card-close" :aria-label="text('Close', 'Fermer')" @click="emit('close')">
        ✕
      </button>
    </header>

    <dl class="map-card-facts">
      <div class="map-card-fact">
        <dt>{{ text('Location', 'Lieu') }}</dt>
        <dd>{{ company.location.raw ?? statusLabel(company.status, currentLocale) }}</dd>
      </div>
      <div v-if="company.is_headquarters" class="map-card-fact">
        <dt>{{ text('Site', 'Site') }}</dt>
        <dd>{{ text('Headquarters', 'Siège social') }}</dd>
      </div>
      <div class="map-card-fact">
        <dt>{{ text('Distance', 'Distance') }}</dt>
        <dd>{{ distanceLabel(company.distance_meters) }}</dd>
      </div>
    </dl>

    <NuxtLink class="btn-ghost map-card-apply" :to="`/companies/${company.company.id}`">
      {{ text('View company →', 'Voir l’entreprise →') }}
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
  min-width: 36px;
  min-height: 36px;
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
.map-card-autopilot-status { color: var(--text-dim); font-size: 12px; margin: 0; }
.map-card-autopilot-error { color: var(--danger, #b91c1c); font-size: 12px; margin: 0; }
@media (max-width: 460px) {
  .map-card { border-radius: var(--radius); }
  .map-card-apply { align-self: stretch; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; }
}
</style>
