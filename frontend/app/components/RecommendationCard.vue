<!--
  One evidence-backed recommendation (§26-33).

  A recommendation is advice, never a lever: it states what it suggests, how much evidence stands
  behind it (the confidence, derived from the weakest cited sample), and the metrics it cites with
  their own numbers — so a reader can trace the claim back to the analytics report. It carries
  nothing it can execute. Acting on it is a separate, human-approved step on the strategy surface;
  this card only explains and points there.
-->
<script setup lang="ts">
import { computed } from 'vue'
import type { CareerRecommendation, RecommendationConfidence } from '~/types/v2'

const props = defineProps<{ recommendation: CareerRecommendation }>()
const { locale, localize: l } = useLocale()

type BadgeColor = 'success' | 'info' | 'neutral'

/** The colour of the confidence badge, mirroring how much evidence backs the suggestion. */
const confidenceColor = computed<BadgeColor>(() => {
  const map: Record<RecommendationConfidence, BadgeColor> = {
    HIGH: 'success',
    MEDIUM: 'info',
    LOW: 'neutral',
  }
  return map[props.recommendation.confidence]
})

/** The kind rendered as words, its underscores softened for a reader. */
const kindLabel = computed(() =>
  locale.value === 'fr'
    ? ({
        PRIORITIZE_ROLE_FAMILY: 'privilégier une famille de métier',
        DEPRIORITIZE_ROLE_FAMILY: 'réduire une famille de métier',
        PRIORITIZE_SOURCE: 'privilégier une source',
        DEPRIORITIZE_SOURCE: 'réduire une source',
        REVIEW_OPPORTUNITY_TYPE_MIX: 'réexaminer les types d’offres',
        REVIEW_DOCUMENT_STRATEGY: 'réexaminer les documents',
        REVIEW_SEARCH_CRITERIA: 'réexaminer les critères',
        REVIEW_INTERVIEW_PREPARATION: 'réexaminer la préparation aux entretiens',
      } as Record<string, string>)[props.recommendation.kind]
      ?? props.recommendation.kind.replace(/_/g, ' ').toLowerCase()
    : props.recommendation.kind.replace(/_/g, ' ').toLowerCase())

const confidenceLabel = computed(() => {
  if (locale.value !== 'fr') return `${props.recommendation.confidence.toLowerCase()} confidence`
  const labels: Record<RecommendationConfidence, string> = {
    HIGH: 'confiance élevée',
    MEDIUM: 'confiance moyenne',
    LOW: 'confiance faible',
  }
  return labels[props.recommendation.confidence]
})

const proposalLink = computed(() => ({
  path: '/strategy',
  query: { recommendation: props.recommendation.id },
}))
</script>

<template>
  <li class="rec">
    <div class="rec__head">
      <span class="rec__kind">{{ kindLabel }}</span>
      <UBadge :color="confidenceColor" variant="subtle" size="xs">
        {{ confidenceLabel }}
      </UBadge>
    </div>
    <p class="rec__summary">
      {{ recommendation.summary }}
    </p>
    <p v-if="recommendation.detail" class="rec__detail">
      {{ recommendation.detail }}
    </p>
    <ul v-if="recommendation.evidence.length > 0" class="rec__evidence">
      <li v-for="item in recommendation.evidence" :key="item.ordinal" class="rec__evidence-item">
        {{ item.detail }}
        <span class="rec__sample">n={{ item.sample_size }}</span>
      </li>
    </ul>
    <NuxtLink
      :to="proposalLink"
      class="rec__action"
      data-test="create-proposal"
    >
      {{ l('Create a proposal', 'Créer une proposition') }}
    </NuxtLink>
  </li>
</template>

<style scoped>
.rec { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.75rem 1rem; display: flex; flex-direction: column; gap: 0.4rem; list-style: none; }
.rec__head { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.rec__kind { font-weight: 600; text-transform: capitalize; }
.rec__summary { margin: 0; }
.rec__detail { margin: 0; color: var(--ui-text-muted, #6b7280); font-size: 0.9rem; }
.rec__evidence { margin: 0.2rem 0 0; padding-left: 1rem; display: flex; flex-direction: column; gap: 0.2rem; }
.rec__evidence-item { font-size: 0.8rem; color: var(--ui-text-muted, #6b7280); }
.rec__sample { font-variant-numeric: tabular-nums; opacity: 0.8; margin-left: 0.3rem; }
.rec__action { align-self: flex-start; min-height: 2.75rem; display: inline-flex; align-items: center; font-size: 0.85rem; font-weight: 600; color: var(--ui-primary, #4f46e5); }
@media (max-width: 560px) {
  .rec { padding: 0.85rem; }
  .rec__action { width: 100%; justify-content: center; border: 1px solid currentColor; border-radius: var(--radius); }
}
</style>
