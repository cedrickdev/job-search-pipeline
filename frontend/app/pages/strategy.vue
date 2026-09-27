<!--
  Strategy: the approval gate — the spine's only mutation, and the one screen where it happens.

  A strategy proposal is a typed, bounded edit to one search or one application policy, drawn from a
  recommendation but carrying its own before → after diff. It changes nothing until a human approves
  it here. The queue at the top is the review set (still-open proposals); each card offers approve and
  dismiss, and a change that loosens a safety brake costs a deliberate second confirmation — the card
  handles that round-trip, the page just lists them. The lower list is the record: every proposal in a
  terminal state, so what was approved, dismissed or let lapse stays visible.

  The rule the whole surface protects: "the system never silently expands the user's application
  policy." It lives on the server and surfaces as the card's second gate; nothing here can bypass it
  (docs/CAREER_INTELLIGENCE.md §The approval gate).
-->
<script setup lang="ts">
import { computed } from 'vue'
import {
  usePendingProposalsQuery,
  useProposalHistoryQuery,
  useStrategyProposalActions,
} from '~/composables/useStrategyProposals'
import type { StrategyChangeProposal } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

definePageMeta({ middleware: 'auth' })

useHead({ title: 'Strategy · Command Center' })

const {
  data: pendingData,
  status: pendingStatus,
  error: pendingError,
  refresh: refreshPending,
} = usePendingProposalsQuery()
const { data: historyData } = useProposalHistoryQuery()
const { approve, dismiss } = useStrategyProposalActions()

const pending = computed<StrategyChangeProposal[]>(() => pendingData.value?.proposals ?? [])
const history = computed<StrategyChangeProposal[]>(() => historyData.value?.proposals ?? [])

const pendingLoading = computed(
  () => pendingStatus.value === 'pending' && !pendingData.value)

// The record is the history minus anything still open — those are already in the queue above.
const settled = computed(() => history.value.filter(p => !p.is_open))
</script>

<template>
  <section class="strategy">
    <header class="strategy__header">
      <h1>Strategy</h1>
      <UButton
        variant="ghost"
        icon="i-heroicons-arrow-path"
        :loading="pendingStatus === 'pending'"
        @click="() => refreshPending()"
      >
        Refresh
      </UButton>
    </header>

    <p class="strategy__muted">
      Each proposal is a single, reviewable change to one search or policy. Nothing here is applied
      until you approve it, and a change that loosens a safety limit asks twice.
    </p>

    <div class="strategy__block" data-test="pending">
      <h2>Awaiting your review</h2>
      <p v-if="pendingError" class="strategy__error" role="alert">
        {{ errorMessage(pendingError) }}
      </p>
      <p v-else-if="pendingLoading" class="strategy__muted">Loading proposals…</p>
      <p v-else-if="pending.length === 0" class="strategy__muted">
        Nothing to review. Generate recommendations on the
        <NuxtLink to="/career">career</NuxtLink> surface — an actionable one becomes a proposal here.
      </p>
      <ul v-else class="strategy__list">
        <StrategyProposalCard
          v-for="proposal in pending"
          :key="proposal.id"
          :proposal="proposal"
          :approve="approve"
          :dismiss="dismiss"
        />
      </ul>
    </div>

    <div v-if="settled.length > 0" class="strategy__block" data-test="history">
      <h2>History</h2>
      <ul class="strategy__list">
        <StrategyProposalCard
          v-for="proposal in settled"
          :key="proposal.id"
          :proposal="proposal"
          :approve="approve"
          :dismiss="dismiss"
        />
      </ul>
    </div>
  </section>
</template>

<style scoped>
.strategy { display: flex; flex-direction: column; gap: 1rem; }
.strategy__header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.strategy__header h1 { font-size: 1.25rem; font-weight: 600; margin: 0; }
.strategy__muted { color: var(--ui-text-muted, #6b7280); font-size: 0.85rem; margin: 0; }
.strategy__block { display: flex; flex-direction: column; gap: 0.6rem; }
.strategy__block h2 { font-size: 0.95rem; font-weight: 600; margin: 0; }
.strategy__error { color: var(--ui-error, #dc2626); font-size: 0.85rem; margin: 0; }
.strategy__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
</style>
