<!--
  One application's append-only audit trail (§41).

  Rendered only while a row is expanded, so its query fetches that application's
  history on demand rather than every application's up front. Each entry is one
  immutable event — created, prepared, gate-evaluated, submitted — with its reasons,
  which is where the "why" behind a REQUIRES_HUMAN or a refusal is read. The trail is
  a record, never an action: nothing here changes anything.
-->
<script setup lang="ts">
import { computed, toRef } from 'vue'
import { useApplicationEventsQuery } from '~/composables/useApplications'
import type { ApplicationEvent } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'
import { applicationEventDetail, applicationReason } from '~/utils/application-event-format'

const { localize: l, locale } = useLocale()
const props = defineProps<{ applicationId: string }>()

const { data, status, error } = useApplicationEventsQuery(
  toRef(props, 'applicationId'), { enabled: true })

const events = computed<ApplicationEvent[]>(() => data.value?.events ?? [])

/** The employer-safe instant, formatted for a reader without leaking a timezone quirk. */
function when(iso: string): string {
  return new Date(iso).toLocaleString(locale.value === 'fr' ? 'fr-CH' : 'en-CH')
}

const EVENT_LABELS: Record<ApplicationEvent['event_type'], [string, string]> = {
  CREATED: ['Created', 'Créée'],
  PREPARATION_STARTED: ['Preparation started', 'Préparation commencée'],
  PREPARED: ['Prepared', 'Préparée'],
  GATE_EVALUATED: ['Policy checked', 'Politique vérifiée'],
  HUMAN_REQUIRED: ['Human review required', 'Intervention humaine requise'],
  APPROVED: ['Approved', 'Approuvée'],
  SUBMISSION_STARTED: ['Submission started', 'Envoi commencé'],
  SUBMITTED: ['Submitted', 'Envoyée'],
  SUBMISSION_STATE_UNKNOWN: ['Submission unconfirmed', 'Envoi non confirmé'],
  FAILED: ['Failed', 'Échec'],
  CANCELLED: ['Cancelled', 'Annulée'],
  WITHDRAWN: ['Withdrawn', 'Retirée'],
  DUPLICATE_BLOCKED: ['Duplicate blocked', 'Doublon bloqué'],
  RATE_LIMITED: ['Rate limit reached', 'Limite atteinte'],
}
const ACTOR_LABELS: Record<ApplicationEvent['actor'], [string, string]> = {
  SYSTEM: ['System', 'Système'],
  USER: ['You', 'Vous'],
  WORKER: ['Worker', 'Automate'],
}
</script>

<template>
  <div class="trail">
    <p v-if="error" class="trail__error" role="alert">{{ errorMessage(error) }}</p>
    <p v-else-if="status === 'pending' && events.length === 0" class="trail__empty">
      {{ l('Loading history…', 'Chargement de l’historique…') }}
    </p>
    <ol v-else class="trail__list">
      <li v-for="(event, index) in events" :key="index" class="trail__event">
        <span class="trail__type">{{ l(...EVENT_LABELS[event.event_type]) }}</span>
        <span class="trail__actor">{{ l(...ACTOR_LABELS[event.actor]) }}</span>
        <span class="trail__when">{{ when(event.occurred_at) }}</span>
        <span v-if="event.detail" class="trail__detail">{{ applicationEventDetail(event.detail, locale) }}</span>
        <ul v-if="event.reasons.length > 0" class="trail__reasons">
          <li v-for="(reason, reasonIndex) in event.reasons" :key="reasonIndex">
            {{ applicationReason(reason.detail ?? reason.code, locale) }}
          </li>
        </ul>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.trail { margin-top: 0.75rem; border-top: 1px dashed var(--ui-border, #e5e7eb); padding-top: 0.5rem; }
.trail__list { display: flex; flex-direction: column; gap: 0.4rem; list-style: none; padding: 0; margin: 0; }
.trail__event { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: baseline; font-size: 0.85rem; }
.trail__type { font-weight: 600; }
.trail__actor, .trail__when { color: var(--ui-text-muted, #6b7280); }
.trail__detail { flex-basis: 100%; color: var(--ui-text-muted, #6b7280); }
.trail__reasons { flex-basis: 100%; margin: 0.2rem 0 0; padding-left: 1rem; color: var(--ui-text-muted, #6b7280); }
.trail__empty { color: var(--ui-text-muted, #6b7280); font-size: 0.85rem; }
.trail__error { color: var(--ui-error, #dc2626); font-size: 0.85rem; }
</style>
