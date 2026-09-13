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
import type { CompanyGeoItem, OpportunityGeoItem } from '~/types/v2'
import { distanceLabel, remoteLabel, statusLabel } from '~/utils/geo-format'

const props = defineProps<{
  opportunity?: OpportunityGeoItem | null
  company?: CompanyGeoItem | null
}>()
const emit = defineEmits<{ close: [] }>()
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

    <a
      v-if="opportunity.application_url"
      class="btn-primary map-card-apply"
      :href="opportunity.application_url"
      target="_blank"
      rel="noopener noreferrer"
    >
      Open posting ↗
    </a>
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
</style>
