<!--
  One proposed action, as a confirm-gated card.

  This is where "prose has zero authority" meets the screen. The card shows two things
  and they come from different places: `summary` is the sentence the model wrote to
  explain itself — shown as plain text, never as trusted intent — and the action label
  below it is derived from the *typed* `ChatAction` by `describeChatAction`, so what the
  card says confirming will do is read off validated fields, not off the model's prose.

  The card never acts. Confirm and Dismiss emit to the page, which asks the store, which
  asks the server, which re-runs every gate. A card is actionable only while its status
  is `PROPOSED`; once a decision has been recorded the buttons are gone and a badge shows
  where it landed — `EXECUTED`, `REJECTED` (refused at the gate — a proposal is not a
  permission), `FAILED` or `DISMISSED` (docs/CAREER_CHAT.md §The last gate).
-->
<script setup lang="ts">
import { computed } from 'vue'
import type { ChatActionProposal, ChatActionProposalStatus } from '~/types/v2'
import { describeChatAction } from '~/utils/v2-chat'

const props = defineProps<{ proposal: ChatActionProposal, busy?: boolean }>()
const emit = defineEmits<{ confirm: [], dismiss: [] }>()

const label = computed(() => describeChatAction(props.proposal.action))
const isOpen = computed(() => props.proposal.status === 'PROPOSED')

type BadgeColor = 'primary' | 'success' | 'error' | 'warning' | 'neutral'

/** The colour a terminal status takes, mirroring how the execution landed. */
const STATUS_COLOR: Record<ChatActionProposalStatus, BadgeColor> = {
  PROPOSED: 'primary',
  EXECUTED: 'success',
  REJECTED: 'error',
  FAILED: 'error',
  DISMISSED: 'neutral',
}
</script>

<template>
  <div class="action-card" role="group" aria-label="Proposed action">
    <div class="action-card__head">
      <span class="action-card__what">{{ label }}</span>
      <UBadge
        v-if="!isOpen"
        :color="STATUS_COLOR[proposal.status]"
        variant="subtle"
        size="sm"
      >
        {{ proposal.status }}
      </UBadge>
    </div>

    <p class="action-card__summary">{{ proposal.summary }}</p>

    <div v-if="isOpen" class="action-card__btns">
      <UButton
        size="xs"
        color="primary"
        :loading="busy"
        :disabled="busy"
        @click="emit('confirm')"
      >
        Confirm
      </UButton>
      <UButton
        size="xs"
        variant="ghost"
        :disabled="busy"
        @click="emit('dismiss')"
      >
        Dismiss
      </UButton>
    </div>
    <p v-else class="action-card__note">
      Nothing changes without your confirmation.
    </p>
  </div>
</template>

<style scoped>
.action-card { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.75rem 1rem; display: flex; flex-direction: column; gap: 0.5rem; }
.action-card__head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.action-card__what { font-weight: 600; }
.action-card__summary { font-size: 0.9rem; color: var(--ui-text-muted, #6b7280); margin: 0; }
.action-card__btns { display: flex; gap: 0.5rem; }
.action-card__note { font-size: 0.8rem; color: var(--ui-text-muted, #6b7280); margin: 0; }
</style>
