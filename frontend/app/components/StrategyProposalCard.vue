<!--
  One strategy proposal — the spine's only mutation, gated behind a human (§34-45).

  A proposal is a typed, bounded description of an edit to one search or one application policy. The
  card shows the whole change as a before → after diff (never the raw levers), whether it loosens a
  safety brake (`is_sensitive`), and — while it is still open — the two verbs a reviewer has: approve
  and dismiss.

  The acceptance rule lives in the approve path. A plain approval of a loosening change is refused by
  the server with `sensitive_confirmation_required`; the card catches exactly that code and swaps in a
  deliberate second confirmation rather than surfacing it as a generic error. Only that second,
  explicit "yes" sends `confirm_sensitive`. "The system never silently expands the user's application
  policy" is this two-step. Every other refusal is shown as its sentence and the card stays put.
-->
<script setup lang="ts">
import { ref } from 'vue'
import type {
  StrategyChangeProposal,
  StrategyChangeProposalStatus,
} from '~/types/v2'
import type { ApproveArgs } from '~/composables/useStrategyProposals'
import type { Mutation } from '~/composables/useMutation'
import type { StrategyChangeExecution } from '~/types/v2'
import { ApiError } from '~/utils/api-client'
import { errorMessage } from '~/utils/v2-errors'

const props = defineProps<{
  proposal: StrategyChangeProposal
  approve: Mutation<ApproveArgs, StrategyChangeExecution>
  dismiss: Mutation<string, StrategyChangeProposal>
}>()

// Per-card state, so one card's in-flight action or refusal never shows on another.
const busy = ref(false)
const rowError = ref<string | null>(null)
// Set once a plain approval of a loosening change is refused; flips the card into its second-gate
// prompt. A non-sensitive proposal never reaches this.
const awaitingSensitiveConfirm = ref(false)

type BadgeColor = 'primary' | 'success' | 'error' | 'warning' | 'neutral'

const STATUS_COLOR: Record<StrategyChangeProposalStatus, BadgeColor> = {
  PROPOSED: 'primary',
  EXECUTED: 'success',
  REJECTED: 'error',
  FAILED: 'error',
  DISMISSED: 'neutral',
  EXPIRED: 'neutral',
}

function label(value: string): string {
  return value.replace(/_/g, ' ').toLowerCase()
}

async function onApprove(confirmSensitive: boolean): Promise<void> {
  rowError.value = null
  busy.value = true
  try {
    await props.approve.mutateAsync({ proposalId: props.proposal.id, confirmSensitive })
    awaitingSensitiveConfirm.value = false
  }
  catch (caught) {
    // The one code that is a prompt, not a failure: show the second-gate confirmation.
    if (caught instanceof ApiError && caught.code === 'sensitive_confirmation_required') {
      awaitingSensitiveConfirm.value = true
    }
    else {
      rowError.value = errorMessage(caught)
    }
  }
  finally {
    busy.value = false
  }
}

async function onDismiss(): Promise<void> {
  rowError.value = null
  busy.value = true
  try {
    await props.dismiss.mutateAsync(props.proposal.id)
  }
  catch (caught) {
    rowError.value = errorMessage(caught)
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <li class="proposal">
    <div class="proposal__head">
      <span class="proposal__kind">{{ label(proposal.change_kind) }}</span>
      <span class="proposal__target">on {{ label(proposal.target) }}</span>
      <UBadge :color="STATUS_COLOR[proposal.status]" variant="subtle" size="xs">
        {{ proposal.status.toLowerCase() }}
      </UBadge>
      <UBadge
        v-if="proposal.is_sensitive"
        color="warning"
        variant="subtle"
        size="xs"
        data-test="sensitive-badge"
      >
        loosens a safety limit
      </UBadge>
    </div>

    <p class="proposal__summary">
      {{ proposal.summary }}
    </p>

    <ul class="proposal__diff">
      <li v-for="change in proposal.field_changes" :key="change.field" class="proposal__field">
        <span class="proposal__field-name">{{ label(change.field) }}</span>
        <span class="proposal__before">{{ change.before }}</span>
        <span class="proposal__arrow">→</span>
        <span class="proposal__after">{{ change.after }}</span>
      </li>
    </ul>

    <p v-if="rowError" class="proposal__error" role="alert">
      {{ rowError }}
    </p>

    <!-- The second gate: shown only after the server refused a plain approval of a loosening change. -->
    <div v-if="awaitingSensitiveConfirm" class="proposal__confirm" role="alert">
      <p class="proposal__confirm-text">
        This change loosens a safety limit. Confirm again to apply it.
      </p>
      <div class="proposal__actions">
        <UButton
          size="xs"
          color="warning"
          :loading="busy"
          data-test="confirm-sensitive"
          @click="() => onApprove(true)"
        >
          Confirm &amp; apply
        </UButton>
        <UButton
          size="xs"
          variant="ghost"
          :disabled="busy"
          @click="() => (awaitingSensitiveConfirm = false)"
        >
          Keep it as is
        </UButton>
      </div>
    </div>

    <div v-else-if="proposal.is_open" class="proposal__actions">
      <UButton
        size="xs"
        color="success"
        :loading="busy"
        data-test="approve"
        @click="() => onApprove(false)"
      >
        Approve
      </UButton>
      <UButton
        size="xs"
        color="error"
        variant="ghost"
        :disabled="busy"
        data-test="dismiss"
        @click="onDismiss"
      >
        Dismiss
      </UButton>
    </div>
  </li>
</template>

<style scoped>
.proposal { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.75rem 1rem; display: flex; flex-direction: column; gap: 0.5rem; list-style: none; }
.proposal__head { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.proposal__kind { font-weight: 600; text-transform: capitalize; }
.proposal__target { color: var(--ui-text-muted, #6b7280); font-size: 0.85rem; }
.proposal__summary { margin: 0; }
.proposal__diff { margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.25rem; list-style: none; }
.proposal__field { display: flex; align-items: baseline; gap: 0.5rem; font-size: 0.85rem; flex-wrap: wrap; }
.proposal__field-name { font-weight: 600; text-transform: capitalize; min-width: 8rem; }
.proposal__before { color: var(--ui-text-muted, #6b7280); text-decoration: line-through; }
.proposal__after { color: var(--ui-text, #111827); font-weight: 600; }
.proposal__arrow { color: var(--ui-text-muted, #6b7280); }
.proposal__actions { display: flex; align-items: center; gap: 0.5rem; }
.proposal__error { color: var(--ui-error, #dc2626); font-size: 0.85rem; margin: 0; }
.proposal__confirm { border: 1px solid var(--ui-warning, #f59e0b); border-radius: 0.4rem; padding: 0.5rem 0.75rem; display: flex; flex-direction: column; gap: 0.4rem; }
.proposal__confirm-text { margin: 0; font-size: 0.85rem; }
</style>
