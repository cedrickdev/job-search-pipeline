<!--
  Applications: the last stage of the funnel, where a decided opportunity becomes an
  audited application.

  This screen operates the lifecycle; it never overrides it. Each row shows the state
  the server returned and offers the one action that state allows — prepare a planned
  one, approve and submit a reviewed one, submit an approved one, cancel anything not
  yet sent. The safety rule the whole engine exists for lives on the server (nothing is
  submitted that was not cleared, and the gate is re-checked at submission time,
  docs/APPLICATION_ENGINE.md §1, §5); the UI's job is to make the current state and its
  one next step legible.

  Two things are deliberate.

  **"Approve & submit" is one button, two audited acts.** A reviewed application is
  approved and then submitted in sequence, because that is the single decision a person
  makes on the review screen (§89). Each step is still its own event in the trail.

  **The trail is shown on demand.** A row shows its state; expanding it fetches that
  application's append-only history — created, prepared, gate-evaluated, submitted — so
  the "why" behind a REQUIRES_HUMAN or a refusal is one click away without loading every
  application's events up front.
-->
<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import {
  useApplicationActions,
  useApplicationsQuery,
} from '~/composables/useApplications'
import type { Application, ApplicationState } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

definePageMeta({ middleware: 'auth' })

useHead({ title: 'Applications · Command Center' })

const { data, status, error, refresh } = useApplicationsQuery()
const { prepare, approve, submit, cancel } = useApplicationActions()

const loading = computed(() => status.value === 'pending' && !data.value)
const applications = computed<Application[]>(() => data.value?.applications ?? [])

// The row whose trail is expanded, by id; null when none is open.
const openTrailId = ref<string | null>(null)
// The last action error per application, so a refusal shows on the row that caused it.
const rowErrors = reactive<Record<string, string>>({})
// Which application is mid-action, so only its buttons show a pending state.
const busyId = ref<string | null>(null)

// The states a caller can still act on, so a button is offered only where it applies.
const CAN_PREPARE = new Set<ApplicationState>(['PLANNED', 'REQUIRES_HUMAN', 'FAILED'])
const TERMINAL = new Set<ApplicationState>(['CANCELLED', 'WITHDRAWN'])

function canPrepare(app: Application): boolean {
  return CAN_PREPARE.has(app.state)
}
function canApproveAndSubmit(app: Application): boolean {
  return app.state === 'READY_FOR_REVIEW'
}
function canSubmit(app: Application): boolean {
  return app.state === 'APPROVED'
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
  busyId.value = app.id
  try {
    await action.mutateAsync(app.id)
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
  busyId.value = app.id
  try {
    await approve.mutateAsync(app.id)
    await submit.mutateAsync(app.id)
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
      <h1>Applications</h1>
      <UButton
        variant="ghost"
        icon="i-heroicons-arrow-path"
        :loading="status === 'pending'"
        @click="() => refresh()"
      >
        Refresh
      </UButton>
    </header>

    <p v-if="error" class="applications__error" role="alert">
      {{ errorMessage(error) }}
    </p>

    <p v-else-if="loading" class="applications__empty">Loading applications…</p>

    <p v-else-if="applications.length === 0" class="applications__empty">
      No applications yet. Decide on an opportunity, then open an application for it.
    </p>

    <ul v-else class="applications__list">
      <li v-for="app in applications" :key="app.id" class="application">
        <div class="application__row">
          <div class="application__facts">
            <UBadge :color="stateColor(app.state)" variant="subtle">
              {{ app.state }}
            </UBadge>
            <span class="application__channel">{{ app.channel }}</span>
            <span class="application__target">
              {{ app.opportunity_id ? 'Opportunity' : 'Company' }}
            </span>
            <span v-if="app.attempt_count > 0" class="application__attempts">
              {{ app.attempt_count }} attempt(s)
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
              Prepare
            </UButton>
            <UButton
              v-if="canApproveAndSubmit(app)"
              size="xs"
              color="success"
              :loading="busyId === app.id"
              :disabled="busyId !== null"
              @click="() => approveAndSubmit(app)"
            >
              Approve &amp; submit
            </UButton>
            <UButton
              v-if="canSubmit(app)"
              size="xs"
              color="success"
              :loading="busyId === app.id"
              :disabled="busyId !== null"
              @click="() => run(app, submit)"
            >
              Submit
            </UButton>
            <UButton
              v-if="canCancel(app)"
              size="xs"
              color="error"
              variant="ghost"
              :disabled="busyId !== null"
              @click="() => run(app, cancel)"
            >
              Cancel
            </UButton>
            <UButton
              size="xs"
              variant="ghost"
              @click="() => toggleTrail(app.id)"
            >
              {{ openTrailId === app.id ? 'Hide history' : 'History' }}
            </UButton>
          </div>
        </div>

        <p v-if="rowErrors[app.id]" class="application__error" role="alert">
          {{ rowErrors[app.id] }}
        </p>

        <ApplicationTrail v-if="openTrailId === app.id" :application-id="app.id" />
      </li>
    </ul>
  </section>
</template>

<style scoped>
.applications { display: flex; flex-direction: column; gap: 1rem; }
.applications__header { display: flex; align-items: center; justify-content: space-between; }
.applications__list { display: flex; flex-direction: column; gap: 0.75rem; list-style: none; padding: 0; }
.application { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.75rem 1rem; }
.application__row { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.application__facts { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.application__actions { display: flex; align-items: center; gap: 0.5rem; }
.application__channel, .application__target, .application__attempts { font-size: 0.85rem; color: var(--ui-text-muted, #6b7280); }
.application__error { color: var(--ui-error, #dc2626); font-size: 0.85rem; margin-top: 0.5rem; }
.applications__empty { color: var(--ui-text-muted, #6b7280); }
.applications__error { color: var(--ui-error, #dc2626); }
</style>
