<!--
  One application's real-world outcomes (§observe, §the separation).

  Rendered only while an application row is expanded, so its query fetches that application's
  timeline on demand rather than every application's up front — the same shape as the audit trail.
  Each row is a hiring milestone the outside world produced: an acknowledgement, a screen, an
  interview, an offer, a rejection. The load-bearing rule is what this component cannot do — nothing
  here reads or writes the Phase 12 `ApplicationState`. A recruiter's "no" is recorded as a
  `REJECTED` outcome and never sets the application to `FAILED` (docs/CAREER_INTELLIGENCE.md).

  Three writes, and none deletes. Record opens a new milestone; correct supersedes a mistaken one
  with a new outcome pointing back at it; retract flips one to `RETRACTED`. The list keeps every
  status, so `SUPERSEDED` and `RETRACTED` rows stay visible alongside effective ones — "we believed
  this, then took it back" is part of the record. Only an effective outcome offers correct/retract.
-->
<script setup lang="ts">
import { computed, ref, toRef } from 'vue'
import {
  useApplicationOutcomesQuery,
  useOutcomeActions,
} from '~/composables/useOutcomes'
import type { ApplicationOutcome, OutcomeKind, OutcomeStatus } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

const { localize: l } = useLocale()
const props = defineProps<{ applicationId: string }>()

const { data, status, error } = useApplicationOutcomesQuery(
  toRef(props, 'applicationId'), { enabled: true })
const { record, correct, retract } = useOutcomeActions()

const outcomes = computed<ApplicationOutcome[]>(() => data.value?.outcomes ?? [])

// The nine hiring milestones; the value is the API enum verbatim, softened only for the label.
const KINDS: OutcomeKind[] = [
  'ACKNOWLEDGED', 'SCREEN', 'ASSESSMENT', 'INTERVIEW',
  'OFFER_RECEIVED', 'OFFER_ACCEPTED', 'OFFER_DECLINED', 'REJECTED', 'WITHDRAWN',
]

type BadgeColor = 'success' | 'neutral' | 'warning'
const STATUS_COLOR: Record<OutcomeStatus, BadgeColor> = {
  EFFECTIVE: 'success',
  SUPERSEDED: 'neutral',
  RETRACTED: 'warning',
}

// The record form. `occurred_at` is a datetime-local value, converted to a tz-aware ISO on send.
const draftKind = ref<OutcomeKind>('ACKNOWLEDGED')
const draftWhen = ref('')
const draftDetail = ref('')
const formError = ref<string | null>(null)

// The correction form, keyed to the outcome it supersedes; null when none is open.
const correctingId = ref<string | null>(null)
const correctKind = ref<OutcomeKind>('ACKNOWLEDGED')
const correctWhen = ref('')
const correctDetail = ref('')
const rowError = ref<string | null>(null)

const KIND_LABELS: Record<OutcomeKind, [string, string]> = {
  ACKNOWLEDGED: ['acknowledged', 'accusé de réception'],
  SCREEN: ['screen', 'présélection'],
  ASSESSMENT: ['assessment', 'évaluation'],
  INTERVIEW: ['interview', 'entretien'],
  OFFER_RECEIVED: ['offer received', 'offre reçue'],
  OFFER_ACCEPTED: ['offer accepted', 'offre acceptée'],
  OFFER_DECLINED: ['offer declined', 'offre refusée'],
  REJECTED: ['rejected', 'refusée'],
  WITHDRAWN: ['withdrawn', 'retirée'],
}
const STATUS_LABELS: Record<OutcomeStatus, [string, string]> = {
  EFFECTIVE: ['effective', 'actif'],
  SUPERSEDED: ['superseded', 'remplacé'],
  RETRACTED: ['retracted', 'rétracté'],
}

function kindLabel(kind: OutcomeKind): string { return l(...KIND_LABELS[kind]) }
function statusLabel(status: OutcomeStatus): string { return l(...STATUS_LABELS[status]) }

function when(iso: string): string {
  return new Date(iso).toLocaleString()
}

/** A datetime-local value as a tz-aware ISO instant; empty means "now". */
function toInstant(local: string): string {
  return (local === '' ? new Date() : new Date(local)).toISOString()
}

async function onRecord(): Promise<void> {
  formError.value = null
  try {
    await record.mutateAsync({
      applicationId: props.applicationId,
      body: {
        kind: draftKind.value,
        occurred_at: toInstant(draftWhen.value),
        source: 'MANUAL_USER',
        detail: draftDetail.value.trim() || null,
      },
    })
    draftWhen.value = ''
    draftDetail.value = ''
  }
  catch (caught) {
    formError.value = errorMessage(caught)
  }
}

function openCorrection(outcome: ApplicationOutcome): void {
  correctingId.value = outcome.id
  correctKind.value = outcome.kind
  correctWhen.value = ''
  correctDetail.value = outcome.detail ?? ''
  rowError.value = null
}

async function onCorrect(): Promise<void> {
  if (correctingId.value === null) return
  rowError.value = null
  try {
    await correct.mutateAsync({
      outcomeId: correctingId.value,
      body: {
        kind: correctKind.value,
        occurred_at: toInstant(correctWhen.value),
        detail: correctDetail.value.trim() || null,
      },
    })
    correctingId.value = null
  }
  catch (caught) {
    rowError.value = errorMessage(caught)
  }
}

async function onRetract(outcome: ApplicationOutcome): Promise<void> {
  rowError.value = null
  try {
    await retract.mutateAsync(outcome.id)
  }
  catch (caught) {
    rowError.value = errorMessage(caught)
  }
}
</script>

<template>
  <div class="outcomes">
    <form class="outcomes__form" data-test="record-outcome" @submit.prevent="onRecord">
      <select v-model="draftKind" :aria-label="l('Outcome milestone', 'Étape de recrutement')" class="outcomes__select">
        <option v-for="k in KINDS" :key="k" :value="k">{{ kindLabel(k) }}</option>
      </select>
      <input
        v-model="draftWhen"
        type="datetime-local"
        :aria-label="l('When it happened', 'Date de l’événement')"
        class="outcomes__when"
      >
      <input
        v-model="draftDetail"
        type="text"
        :aria-label="l('Note (optional)', 'Note (facultative)')"
        :placeholder="l('Note (optional)', 'Note (facultative)')"
        class="outcomes__detail"
      >
      <UButton type="submit" size="xs" :loading="record.isPending" data-test="record-submit">
        {{ l('Record outcome', 'Enregistrer le résultat') }}
      </UButton>
    </form>
    <p v-if="formError" class="outcomes__error" role="alert">{{ formError }}</p>

    <p v-if="error" class="outcomes__error" role="alert">{{ errorMessage(error) }}</p>
    <p v-else-if="status === 'pending' && outcomes.length === 0" class="outcomes__empty">
      {{ l('Loading outcomes…', 'Chargement des résultats…') }}
    </p>
    <p v-else-if="outcomes.length === 0" class="outcomes__empty">
      {{ l('No outcomes yet. A recruiter’s reply — a screen, an interview, an offer, a rejection — is recorded here and never changes the application’s own state.', 'Aucun résultat pour le moment. Une réponse du recruteur — présélection, entretien, offre ou refus — sera consignée ici, sans modifier l’état de la candidature.') }}
    </p>
    <ol v-else class="outcomes__list">
      <li v-for="o in outcomes" :key="o.id" class="outcome" :data-status="o.status">
        <div class="outcome__row">
          <span class="outcome__kind">{{ kindLabel(o.kind) }}</span>
          <UBadge :color="STATUS_COLOR[o.status]" variant="subtle" size="xs">
            {{ statusLabel(o.status) }}
          </UBadge>
          <span class="outcome__when">{{ when(o.occurred_at) }}</span>
          <span v-if="o.is_correction" class="outcome__tag">{{ l('correction', 'correction') }}</span>
        </div>
        <p v-if="o.detail" class="outcome__detail">{{ o.detail }}</p>

        <div v-if="o.is_effective && correctingId !== o.id" class="outcome__actions">
          <UButton size="xs" variant="ghost" data-test="correct" @click="() => openCorrection(o)">
            {{ l('Correct', 'Corriger') }}
          </UButton>
          <UButton
            size="xs"
            color="error"
            variant="ghost"
            :loading="retract.isPending"
            data-test="retract"
            @click="() => onRetract(o)"
          >
            {{ l('Retract', 'Rétracter') }}
          </UButton>
        </div>

        <form
          v-if="correctingId === o.id"
          class="outcomes__form"
          data-test="correct-form"
          @submit.prevent="onCorrect"
        >
          <select v-model="correctKind" :aria-label="l('Corrected milestone', 'Étape corrigée')" class="outcomes__select">
            <option v-for="k in KINDS" :key="k" :value="k">{{ kindLabel(k) }}</option>
          </select>
          <input
            v-model="correctWhen"
            type="datetime-local"
            :aria-label="l('Corrected date', 'Date corrigée')"
            class="outcomes__when"
          >
          <input
            v-model="correctDetail"
            type="text"
            :aria-label="l('Corrected note', 'Note corrigée')"
            class="outcomes__detail"
          >
          <UButton type="submit" size="xs" :loading="correct.isPending" data-test="correct-submit">
            {{ l('Save correction', 'Enregistrer la correction') }}
          </UButton>
          <UButton size="xs" variant="ghost" @click="() => (correctingId = null)">
            {{ l('Cancel', 'Annuler') }}
          </UButton>
        </form>
      </li>
    </ol>
    <p v-if="rowError" class="outcomes__error" role="alert">{{ rowError }}</p>
  </div>
</template>

<style scoped>
.outcomes { margin-top: 0.75rem; border-top: 1px dashed var(--ui-border, #e5e7eb); padding-top: 0.5rem; display: flex; flex-direction: column; gap: 0.5rem; }
.outcomes__form { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; }
.outcomes__select, .outcomes__when, .outcomes__detail { padding: 0.3rem 0.45rem; border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.375rem; font: inherit; font-size: 0.8rem; background: var(--ui-bg, #fff); }
.outcomes__detail { flex: 1 1 12rem; }
.outcomes__list { display: flex; flex-direction: column; gap: 0.4rem; list-style: none; padding: 0; margin: 0; }
.outcome { display: flex; flex-direction: column; gap: 0.25rem; }
.outcome[data-status="SUPERSEDED"], .outcome[data-status="RETRACTED"] { opacity: 0.65; }
.outcome__row { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: baseline; font-size: 0.85rem; }
.outcome__kind { font-weight: 600; text-transform: capitalize; }
.outcome__when { color: var(--ui-text-muted, #6b7280); }
.outcome__tag { font-size: 0.7rem; color: var(--ui-text-muted, #6b7280); text-transform: uppercase; letter-spacing: 0.03em; }
.outcome__detail { margin: 0; font-size: 0.85rem; color: var(--ui-text-muted, #6b7280); }
.outcome__actions { display: flex; gap: 0.4rem; }
.outcomes__empty { color: var(--ui-text-muted, #6b7280); font-size: 0.85rem; margin: 0; }
.outcomes__error { color: var(--ui-error, #dc2626); font-size: 0.85rem; margin: 0; }
@media (max-width: 560px) {
  .outcomes__form > * { flex: 1 1 100%; min-height: 44px; }
  .outcome__actions > * { min-height: 44px; }
}
</style>
