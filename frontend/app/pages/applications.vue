<!--
  Applications: the last stage of the funnel, where a decided opportunity becomes an
  audited application.

  This screen operates the lifecycle; it never overrides it. Each row shows the state
  the server returned and offers the one action that state allows — prepare a planned
  one, approve and submit a reviewed one, submit an approved one, cancel anything not
  yet sent. Browser applications separate approval from durable queueing. The safety
  rule the whole engine exists for lives on the server (nothing is
  submitted that was not cleared, and the gate is re-checked at submission time,
  docs/APPLICATION_ENGINE.md §1, §5); the UI's job is to make the current state and its
  one next step legible.

  Two things are deliberate.

  **"Approve & submit" remains for non-browser channels.** A reviewed browser
  application is only approved; a separate action queues it for the browser worker.
  Queue acceptance is not a submitted receipt.

  **The trail is shown on demand.** A row shows its state; expanding it fetches that
  application's append-only history — created, prepared, gate-evaluated, submitted — so
  the "why" behind a REQUIRES_HUMAN or a refusal is one click away without loading every
  application's events up front. The same expansion also holds the outcome timeline
  (Phase 15): the real-world milestones a recruiter produces — a screen, an interview, an
  offer, a rejection. Those are a separate record and never move the lifecycle state above;
  a rejection is a recorded outcome, not a FAILED application (docs/CAREER_INTELLIGENCE.md).
-->
<script setup lang="ts">
const { t, localize: l } = useLocale()
import { computed, reactive, ref, watch } from 'vue'
import {
  useApplicationActions,
  useApplicationsQuery,
} from '~/composables/useApplications'
import type { Application, ApplicationState } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

definePageMeta({ middleware: 'auth' })

useHead({ title: () => `${l('Applications', 'Candidatures')} · Command Center` })

const { data, status, error, refresh } = useApplicationsQuery()
const { prepare, approve, submit, queue, cancel } = useApplicationActions()

const loading = computed(() => status.value === 'pending' && !data.value)
const applications = computed<Application[]>(() => data.value?.applications ?? [])
const route = useRoute()

// The row whose trail is expanded, by id; null when none is open.
const openTrailId = ref<string | null>(null)
watch([applications, () => route.query.application], ([items, requested]) => {
  if (typeof requested === 'string' && items.some(item => item.id === requested))
    openTrailId.value = requested
}, { immediate: true })
// The last action error per application, so a refusal shows on the row that caused it.
const rowErrors = reactive<Record<string, string>>({})
// A refresh is intentionally asynchronous. Keep the server's returned state beside
// the action so a click always produces immediate, accessible feedback rather than
// looking as if it did nothing while the list revalidates.
const rowSuccesses = reactive<Record<string, string>>({})
const queuedIds = reactive<Record<string, string>>({})
// Which application is mid-action, so only its buttons show a pending state.
const busyId = ref<string | null>(null)

// The states a caller can still act on, so a button is offered only where it applies.
// A human hand-off is not retryable preparation: rerunning would only append
// duplicate audit events while the unresolved manual step remains unchanged.
const CAN_PREPARE = new Set<ApplicationState>(['PLANNED', 'FAILED'])
const TERMINAL = new Set<ApplicationState>(['CANCELLED', 'WITHDRAWN'])
const STATE_LABELS: Record<ApplicationState, [string, string]> = {
  PLANNED: ['Planned', 'Planifiée'],
  PREPARING: ['Preparing', 'En préparation'],
  READY_FOR_REVIEW: ['Ready for review', 'À vérifier'],
  REQUIRES_HUMAN: ['Human review required', 'Vérification humaine requise'],
  APPROVED: ['Approved', 'Approuvée'],
  SUBMITTING: ['Submitting', 'En cours d’envoi'],
  SUBMITTED: ['Submitted', 'Envoyée'],
  SUBMISSION_STATE_UNKNOWN: ['Submission unconfirmed', 'Envoi non confirmé'],
  FAILED: ['Failed', 'Échec'],
  CANCELLED: ['Cancelled', 'Annulée'],
  WITHDRAWN: ['Withdrawn', 'Retirée'],
}

function stateLabel(state: ApplicationState): string {
  return l(...STATE_LABELS[state])
}

function channelLabel(channel: Application['channel']): string {
  const labels: Record<Application['channel'], [string, string]> = {
    MANUAL: ['Manual', 'Manuelle'],
    BROWSER: ['Browser', 'Navigateur'],
    ATS_API: ['ATS API', 'API ATS'],
    ATS_FORM: ['ATS form', 'Formulaire ATS'],
    DIRECT_FORM: ['Direct form', 'Formulaire direct'],
    EMAIL: ['Email', 'Courriel'],
    UNSUPPORTED: ['Unsupported', 'Non pris en charge'],
  }
  return l(...labels[channel])
}

function canPrepare(app: Application): boolean {
  return CAN_PREPARE.has(app.state)
}
function canApproveAndSubmit(app: Application): boolean {
  return app.state === 'READY_FOR_REVIEW' && app.channel !== 'BROWSER'
}
function canApproveBrowser(app: Application): boolean {
  return app.state === 'READY_FOR_REVIEW' && app.channel === 'BROWSER'
}
function canSubmit(app: Application): boolean {
  return app.state === 'APPROVED' && app.channel !== 'BROWSER'
}
function canQueue(app: Application): boolean {
  return app.state === 'APPROVED' && app.channel === 'BROWSER'
}
function canCancel(app: Application): boolean {
  return !TERMINAL.has(app.state) && app.state !== 'SUBMITTED'
}

type BadgeColor = 'primary' | 'success' | 'error' | 'warning' | 'info' | 'neutral'

/** The colour a state badge takes, mirroring how far the application has gone. */
function stateColor(state: ApplicationState): BadgeColor {
  if (state === 'SUBMITTED') return 'success'
  if (state === 'FAILED' || state === 'SUBMISSION_STATE_UNKNOWN') return 'error'
  if (state === 'REQUIRES_HUMAN' || state === 'READY_FOR_REVIEW') return 'warning'
  if (TERMINAL.has(state)) return 'neutral'
  return 'info'
}

async function run(
  app: Application,
  action: { mutateAsync: (id: string) => Promise<Application> },
): Promise<void> {
  delete rowErrors[app.id]
  delete rowSuccesses[app.id]
  busyId.value = app.id
  try {
    const updated = await action.mutateAsync(app.id)
    rowSuccesses[app.id] = updated.state === 'REQUIRES_HUMAN'
      ? l(
          'Preparation is complete and needs your review before anything else happens.',
          'La préparation est terminée et nécessite votre vérification avant toute autre étape.',
        )
      : l(
          `Application updated: ${stateLabel(updated.state)}.`,
          `Candidature mise à jour : ${stateLabel(updated.state)}.`,
        )
  }
  catch (caught) {
    rowErrors[app.id] = errorMessage(caught)
  }
  finally {
    busyId.value = null
  }
}

/** Approve then submit — the review screen's single decision, two audited acts (§89). */
async function approveAndSubmit(app: Application): Promise<void> {
  delete rowErrors[app.id]
  delete rowSuccesses[app.id]
  busyId.value = app.id
  try {
    await approve.mutateAsync(app.id)
    const updated = await submit.mutateAsync(app.id)
    rowSuccesses[app.id] = l(
      `Application updated: ${stateLabel(updated.state)}.`,
      `Candidature mise à jour : ${stateLabel(updated.state)}.`,
    )
  }
  catch (caught) {
    rowErrors[app.id] = errorMessage(caught)
  }
  finally {
    busyId.value = null
  }
}

async function queueBrowser(app: Application): Promise<void> {
  delete rowErrors[app.id]
  delete rowSuccesses[app.id]
  busyId.value = app.id
  try {
    const result = await queue.mutateAsync(app.id)
    queuedIds[app.id] = result.task_id
    rowSuccesses[app.id] = l(
      'Browser preparation is queued. It will stop for CAPTCHA, MFA, sign-in, or an uncertain form.',
      'La préparation navigateur est mise en file. Elle s’arrêtera devant un CAPTCHA, une MFA, une connexion ou un formulaire incertain.',
    )
  }
  catch (caught) {
    rowErrors[app.id] = errorMessage(caught)
  }
  finally {
    busyId.value = null
  }
}

function toggleTrail(applicationId: string): void {
  openTrailId.value = openTrailId.value === applicationId ? null : applicationId
}
</script>

<template>
  <section class="applications">
    <header class="applications__header">
      <h1>{{ t('page.applications') }}</h1>
      <UButton
        variant="ghost"
        icon="i-heroicons-arrow-path"
        :loading="status === 'pending'"
        @click="() => refresh()"
      >
        {{ l('Refresh', 'Actualiser') }}
      </UButton>
    </header>

    <p v-if="error" class="applications__error" role="alert">
      {{ errorMessage(error) }}
    </p>

    <p v-else-if="loading" class="applications__empty">{{ l('Loading applications…', 'Chargement des candidatures…') }}</p>

    <p v-else-if="applications.length === 0" class="applications__empty">
      {{ l('No applications yet. Decide on an opportunity, then open an application for it.', 'Aucune candidature pour le moment. Choisissez une offre, puis ouvrez une candidature correspondante.') }}
    </p>

    <ul v-else class="applications__list">
      <li v-for="app in applications" :id="`application-${app.id}`" :key="app.id" class="application">
        <div class="application__row">
          <div class="application__facts">
            <UBadge :color="stateColor(app.state)" variant="subtle">
              {{ stateLabel(app.state) }}
            </UBadge>
            <span class="application__channel">{{ channelLabel(app.channel) }}</span>
            <span class="application__target">
              {{ app.opportunity_id ? l('Opportunity', 'Offre') : l('Company', 'Entreprise') }}
            </span>
            <span v-if="app.attempt_count > 0" class="application__attempts">
              {{ app.attempt_count }} {{ l('attempt(s)', 'tentative(s)') }}
            </span>
          </div>

          <div class="application__actions">
            <UButton
              v-if="canPrepare(app)"
              size="xs"
              :loading="busyId === app.id"
              :disabled="busyId !== null"
              @click="() => run(app, prepare)"
            >
              {{ l('Prepare', 'Préparer') }}
            </UButton>
            <UButton
              v-if="canApproveAndSubmit(app)"
              size="xs"
              color="success"
              :loading="busyId === app.id"
              :disabled="busyId !== null"
              @click="() => approveAndSubmit(app)"
            >
              {{ l('Approve & submit', 'Approuver et envoyer') }}
            </UButton>
            <UButton
              v-if="canApproveBrowser(app)"
              size="xs"
              :loading="busyId === app.id"
              :disabled="busyId !== null"
              @click="() => run(app, approve)"
            >
              {{ l('Approve', 'Approuver') }}
            </UButton>
            <UButton
              v-if="canSubmit(app)"
              size="xs"
              color="success"
              :loading="busyId === app.id"
              :disabled="busyId !== null"
              @click="() => run(app, submit)"
            >
              {{ l('Submit', 'Envoyer') }}
            </UButton>
            <UButton
              v-if="canQueue(app)"
              size="xs"
              color="success"
              :loading="busyId === app.id"
              :disabled="busyId !== null || Boolean(queuedIds[app.id])"
              @click="() => queueBrowser(app)"
            >
              {{ l('Queue browser submission', 'Mettre en file pour envoi navigateur') }}
            </UButton>
            <UButton
              v-if="canCancel(app)"
              size="xs"
              color="error"
              variant="ghost"
              :disabled="busyId !== null"
              @click="() => run(app, cancel)"
            >
              {{ l('Cancel', 'Annuler') }}
            </UButton>
            <UButton
              size="xs"
              variant="ghost"
              @click="() => toggleTrail(app.id)"
            >
              {{ openTrailId === app.id ? l('Hide history', 'Masquer l’historique') : l('History', 'Historique') }}
            </UButton>
          </div>
        </div>

        <p v-if="rowErrors[app.id]" class="application__error" role="alert">
          {{ rowErrors[app.id] }}
        </p>
        <p v-if="rowSuccesses[app.id]" class="application__success" role="status">
          {{ rowSuccesses[app.id] }}
        </p>
        <p v-if="app.state === 'REQUIRES_HUMAN'" class="application__notice" role="status">
          {{ l('Preparation is complete, but this channel needs your manual review. Open the posting and the execution history below; do not prepare it again.', 'La préparation est terminée, mais ce canal exige votre vérification manuelle. Ouvrez l’offre et le journal ci-dessous ; ne relancez pas la préparation.') }}
        </p>
        <p v-if="queuedIds[app.id]" role="status">
          {{ l('Queued for browser processing. Refresh to see the resulting application state.', 'Mise en file pour traitement par navigateur. Actualisez pour voir le nouvel état de la candidature.') }}
        </p>

        <div v-if="openTrailId === app.id" class="application__expanded">
          <section class="application__section">
            <h3 class="application__section-title">{{ l('Execution trail', 'Journal d’exécution') }}</h3>
            <ApplicationTrail :application-id="app.id" />
          </section>
          <section class="application__section">
            <h3 class="application__section-title">{{ l('Outcomes', 'Résultats') }}</h3>
            <OutcomeTimeline :application-id="app.id" />
          </section>
        </div>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.applications { display: flex; flex-direction: column; gap: 1rem; }
.applications__header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.applications__list { display: flex; flex-direction: column; gap: 0.75rem; list-style: none; padding: 0; }
.application { border: 1px solid var(--ui-border, #e5e7eb); border-radius: var(--radius); padding: 1rem; background: var(--surface); box-shadow: 0 1px 2px rgb(0 0 0 / 8%); }
.application__row { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.application__facts { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.application__actions { display: flex; align-items: center; gap: 0.5rem; }
.application__channel, .application__target, .application__attempts { font-size: 0.85rem; color: var(--ui-text-muted, #6b7280); }
.application__error { color: var(--ui-error, #dc2626); font-size: 0.85rem; margin-top: 0.5rem; }
.application__success { color: var(--ui-success, #15803d); font-size: 0.85rem; margin-top: 0.5rem; }
.application__notice { color: var(--ui-text-muted, #6b7280); font-size: 0.85rem; margin-top: 0.5rem; }
.application__expanded { display: flex; flex-direction: column; gap: 0.5rem; }
.application__section { display: flex; flex-direction: column; }
.application__section-title { font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; color: var(--ui-text-muted, #6b7280); margin: 0.5rem 0 0; }
.applications__empty { color: var(--ui-text-muted, #6b7280); }
.applications__error { color: var(--ui-error, #dc2626); }
@media (max-width: 620px) {
  .applications__header { align-items: flex-start; flex-direction: column; gap: 0.75rem; }
  .application__actions { width: 100%; flex-wrap: wrap; }
  .application__actions > * { min-height: 44px; }
  .application__actions > * { flex: 1 1 auto; }
  .application { padding: 0.875rem; }
}
</style>
