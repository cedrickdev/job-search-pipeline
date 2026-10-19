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
import { computed, ref, watch } from 'vue'
import { useSearchProfilesQuery } from '~/composables/useAccount'
import {
  usePendingProposalsQuery,
  useProposeStrategyChange,
  useProposalHistoryQuery,
  useStrategyProposalActions,
} from '~/composables/useStrategyProposals'
import type {
  SearchProfile,
  StrategyChange,
  StrategyChangeProposal,
} from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

const { t, localize: l } = useLocale()
const route = useRoute()
definePageMeta({ middleware: 'auth' })

useHead({ title: () => `${l('Strategy', 'Stratégie')} · Command Center` })

const {
  data: pendingData,
  status: pendingStatus,
  error: pendingError,
  refresh: refreshPending,
} = usePendingProposalsQuery()
const { data: historyData, error: historyError, status: historyStatus, refresh: refreshHistory } = useProposalHistoryQuery()
const { data: searchesData, status: searchesStatus } = useSearchProfilesQuery()
const propose = useProposeStrategyChange()
const { approve, dismiss } = useStrategyProposalActions()

const pending = computed<StrategyChangeProposal[]>(() => pendingData.value?.proposals ?? [])
const history = computed<StrategyChangeProposal[]>(() => historyData.value?.proposals ?? [])

const pendingLoading = computed(
  () => pendingStatus.value === 'pending' && !pendingData.value)

// The record is the history minus anything still open — those are already in the queue above.
const settled = computed(() => history.value.filter(p => !p.is_open))
const searches = computed(() => searchesData.value?.search_profiles ?? [])

type SearchChangeKind =
  | 'SET_SEARCH_KEYWORDS'
  | 'SET_SEARCH_SOURCES'
  | 'SET_SEARCH_OPPORTUNITY_TYPES'
  | 'SET_SEARCH_RADIUS'

const selectedProfileId = ref('')
const changeKind = ref<SearchChangeKind>('SET_SEARCH_KEYWORDS')
const summary = ref('')
const titleKeywords = ref('')
const excludedKeywords = ref('')
const sourceKeys = ref('')
const opportunityTypes = ref<string[]>([])
const radiusKm = ref<number | null>(null)
const proposalFormError = ref<string | null>(null)

const sourceRecommendationId = computed(() =>
  typeof route.query.recommendation === 'string' ? route.query.recommendation : null)
const selectedProfile = computed<SearchProfile | null>(() =>
  searches.value.find(profile => profile.id === selectedProfileId.value) ?? null)
const radiusArea = computed(() =>
  selectedProfile.value?.search.areas.find(area => area.kind === 'RADIUS') ?? null)
const profileItems = computed(() => searches.value.map(profile => ({
  value: profile.id,
  label: profile.search.name,
})))
const changeItems = computed(() => [
  { value: 'SET_SEARCH_KEYWORDS', label: l('Keywords', 'Mots-clés') },
  { value: 'SET_SEARCH_SOURCES', label: l('Job-board sources', 'Sources d’offres') },
  { value: 'SET_SEARCH_OPPORTUNITY_TYPES', label: l('Opportunity types', 'Types d’offres') },
  { value: 'SET_SEARCH_RADIUS', label: l('Search radius', 'Rayon de recherche') },
])
const opportunityTypeItems = [
  'FULL_TIME', 'PART_TIME', 'STUDENT_JOB', 'INTERNSHIP', 'APPRENTICESHIP',
  'WORK_STUDY', 'GRADUATE', 'TEMPORARY', 'FREELANCE',
] as const

function lines(value: string): string[] {
  return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean)
}

function hydrateForm(profile: SearchProfile | null): void {
  if (!profile) return
  titleKeywords.value = profile.search.title_keywords.join('\n')
  excludedKeywords.value = profile.search.excluded_keywords.join('\n')
  sourceKeys.value = profile.search.source_keys.join('\n')
  opportunityTypes.value = [...profile.search.opportunity_types]
  const area = profile.search.areas.find(item => item.kind === 'RADIUS')
  radiusKm.value = area?.radius_km ?? null
}

watch(searches, (available) => {
  if (!selectedProfileId.value && available[0]) selectedProfileId.value = available[0].id
}, { immediate: true })
watch(selectedProfile, hydrateForm, { immediate: true })

function buildChange(profile: SearchProfile): StrategyChange | null {
  if (changeKind.value === 'SET_SEARCH_KEYWORDS') {
    return {
      kind: changeKind.value,
      search_profile_id: profile.id,
      before_title_keywords: profile.search.title_keywords,
      title_keywords: lines(titleKeywords.value),
      before_excluded_keywords: profile.search.excluded_keywords,
      excluded_keywords: lines(excludedKeywords.value),
    }
  }
  if (changeKind.value === 'SET_SEARCH_SOURCES') {
    return {
      kind: changeKind.value,
      search_profile_id: profile.id,
      before_source_keys: profile.search.source_keys,
      source_keys: lines(sourceKeys.value),
    }
  }
  if (changeKind.value === 'SET_SEARCH_OPPORTUNITY_TYPES') {
    return {
      kind: changeKind.value,
      search_profile_id: profile.id,
      before_opportunity_types: profile.search.opportunity_types,
      opportunity_types: opportunityTypes.value as typeof profile.search.opportunity_types,
    }
  }
  const beforeRadius = radiusArea.value?.radius_km
  if (beforeRadius === undefined || radiusKm.value === null) return null
  return {
    kind: 'SET_SEARCH_RADIUS',
    search_profile_id: profile.id,
    before_radius_km: beforeRadius,
    radius_km: radiusKm.value,
  }
}

async function onPropose(): Promise<void> {
  proposalFormError.value = null
  const profile = selectedProfile.value
  const change = profile ? buildChange(profile) : null
  if (!profile || !change) {
    proposalFormError.value = l(
      'Choose a compatible saved search and provide the requested values.',
      'Choisissez une recherche compatible et renseignez les valeurs demandées.',
    )
    return
  }
  if (!summary.value.trim()) {
    proposalFormError.value = l('Describe the proposed change.', 'Décrivez la modification proposée.')
    return
  }
  try {
    await propose.mutateAsync({
      change,
      summary: summary.value.trim(),
      source_recommendation_id: sourceRecommendationId.value,
    })
    summary.value = ''
  }
  catch (caught) {
    proposalFormError.value = errorMessage(caught)
  }
}

async function refreshAll() {
  await Promise.all([refreshPending(), refreshHistory()])
}
</script>

<template>
  <section class="strategy">
    <header class="strategy__header">
      <h1>{{ t('page.strategy') }}</h1>
      <UButton
        variant="ghost"
        icon="i-heroicons-arrow-path"
        :loading="pendingStatus === 'pending' || historyStatus === 'pending'"
        @click="refreshAll"
      >
        {{ l('Refresh', 'Actualiser') }}
      </UButton>
    </header>

    <p class="strategy__muted">
      {{ l('Each proposal is a single, reviewable change to one search or policy. Nothing here is applied until you approve it, and a change that loosens a safety limit asks twice.', 'Chaque proposition est une modification unique et vérifiable d’une recherche ou d’une règle. Rien n’est appliqué sans votre accord ; assouplir une limite de sécurité exige une double confirmation.') }}
    </p>

    <div class="strategy__block strategy__block--card" data-test="proposal-builder">
      <div class="strategy__block-head">
        <div>
          <h2>{{ l('Create a reviewable proposal', 'Créer une proposition vérifiable') }}</h2>
          <p class="strategy__muted">
            {{ l('This drafts one exact change. It does not apply it.', 'Cette étape prépare une modification précise. Elle ne l’applique pas.') }}
          </p>
        </div>
        <UBadge v-if="sourceRecommendationId" color="info" variant="subtle">
          {{ l('Linked to recommendation', 'Liée à la recommandation') }}
        </UBadge>
      </div>

      <p v-if="searchesStatus === 'pending'" class="strategy__muted">
        {{ l('Loading saved searches…', 'Chargement des recherches…') }}
      </p>
      <p v-else-if="searches.length === 0" class="strategy__muted">
        {{ l('Create a saved search before drafting a strategy change.', 'Créez une recherche enregistrée avant de proposer une modification de stratégie.') }}
        <NuxtLink to="/profile">{{ l('Open profile', 'Ouvrir le profil') }}</NuxtLink>
      </p>
      <form v-else class="proposal-form" data-test="proposal-form" @submit.prevent="onPropose">
        <UFormField :label="l('Saved search', 'Recherche enregistrée')" required>
          <USelect
            v-model="selectedProfileId"
            :items="profileItems"
            value-key="value"
            :aria-label="l('Saved search', 'Recherche enregistrée')"
          />
        </UFormField>
        <UFormField :label="l('Change', 'Modification')" required>
          <USelect
            v-model="changeKind"
            :items="changeItems"
            value-key="value"
            :aria-label="l('Change type', 'Type de modification')"
          />
        </UFormField>

        <template v-if="changeKind === 'SET_SEARCH_KEYWORDS'">
          <UFormField :label="l('Title keywords', 'Mots-clés du titre')">
            <UTextarea
              v-model="titleKeywords"
              :placeholder="l('One keyword per line', 'Un mot-clé par ligne')"
              data-test="proposal-title-keywords"
            />
          </UFormField>
          <UFormField :label="l('Excluded keywords', 'Mots-clés exclus')">
            <UTextarea
              v-model="excludedKeywords"
              :placeholder="l('One keyword per line', 'Un mot-clé par ligne')"
            />
          </UFormField>
        </template>
        <UFormField v-else-if="changeKind === 'SET_SEARCH_SOURCES'" :label="l('Source keys', 'Clés des sources')">
          <UTextarea v-model="sourceKeys" :placeholder="l('One source per line', 'Une source par ligne')" />
        </UFormField>
        <fieldset v-else-if="changeKind === 'SET_SEARCH_OPPORTUNITY_TYPES'" class="proposal-types">
          <legend>{{ l('Opportunity types', 'Types d’offres') }}</legend>
          <label v-for="type in opportunityTypeItems" :key="type" class="proposal-type">
            <input v-model="opportunityTypes" type="checkbox" :value="type">
            {{ type.replace(/_/g, ' ').toLowerCase() }}
          </label>
        </fieldset>
        <UFormField v-else :label="l('Radius in kilometres', 'Rayon en kilomètres')" required>
          <UInput
            v-model.number="radiusKm"
            type="number"
            min="1"
            max="500"
            :disabled="!radiusArea"
          />
          <p v-if="!radiusArea" class="strategy__muted">
            {{ l('This saved search has no radius area. Choose another change.', 'Cette recherche ne contient aucune zone avec rayon. Choisissez une autre modification.') }}
          </p>
        </UFormField>

        <UFormField class="proposal-form__summary" :label="l('Review summary', 'Résumé à vérifier')" required>
          <UInput
            v-model="summary"
            required
            :placeholder="l('Describe the exact before/after intent', 'Décrivez précisément l’intention avant/après')"
            data-test="proposal-summary"
          />
        </UFormField>
        <p v-if="proposalFormError" class="strategy__error proposal-form__wide" role="alert">
          {{ proposalFormError }}
        </p>
        <p
          v-else-if="propose.isSuccess"
          class="strategy__success proposal-form__wide"
          role="status"
          data-test="proposal-created"
        >
          {{ l('Proposal created. Review and approve it below to apply the change.', 'Proposition créée. Vérifiez-la puis approuvez-la ci-dessous pour appliquer la modification.') }}
        </p>
        <div class="proposal-form__actions proposal-form__wide">
          <UButton type="submit" :loading="propose.isPending" data-test="proposal-submit">
            {{ l('Create proposal', 'Créer la proposition') }}
          </UButton>
        </div>
      </form>
    </div>

    <div class="strategy__block" data-test="pending">
      <h2>{{ l('Awaiting your review', 'En attente de votre validation') }}</h2>
      <p v-if="pendingError" class="strategy__error" role="alert">
        {{ errorMessage(pendingError) }}
      </p>
      <p v-else-if="pendingLoading" class="strategy__muted">{{ l('Loading proposals…', 'Chargement des propositions…') }}</p>
      <p v-else-if="pending.length === 0" class="strategy__muted">
        {{ l('Nothing to review yet. Strategy proposals appear only after the Career screen has enough mature application outcomes (at least 5) to create evidence-backed recommendations.', 'Rien à examiner pour le moment. Les propositions de stratégie apparaissent seulement après que l’écran Carrière dispose d’assez de résultats de candidatures arrivés à maturité (au moins 5) pour produire des recommandations fondées sur des preuves.') }}
        <NuxtLink to="/career">{{ l('Open Career', 'Ouvrir Carrière') }}</NuxtLink>
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

    <div v-if="settled.length > 0 || historyError" class="strategy__block" data-test="history">
      <h2>{{ l('History', 'Historique') }}</h2>
      <p v-if="historyError" class="strategy__error" role="alert">{{ errorMessage(historyError) }}</p>
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
.strategy__block { display: flex; flex-direction: column; gap: 0.75rem; }
.strategy__block--card { border: 1px solid var(--ui-border, #e5e7eb); border-radius: var(--radius); padding: 1rem; background: var(--surface); }
.strategy__block-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; }
.strategy__block h2 { font-size: 0.95rem; font-weight: 600; margin: 0; }
.strategy__error { color: var(--ui-error, #dc2626); font-size: 0.85rem; margin: 0; }
.strategy__success { color: var(--ui-success, #15803d); font-size: 0.85rem; margin: 0; }
.strategy__list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
.proposal-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.85rem; }
.proposal-form__summary, .proposal-form__wide { grid-column: 1 / -1; }
.proposal-form__actions { display: flex; justify-content: flex-end; }
.proposal-types { grid-column: 1 / -1; border: 1px solid var(--ui-border, #e5e7eb); border-radius: var(--radius); padding: 0.75rem; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.5rem; }
.proposal-types legend { padding: 0 0.25rem; font-size: 0.85rem; font-weight: 600; }
.proposal-type { display: flex; align-items: center; gap: 0.5rem; min-height: 2.75rem; font-size: 0.85rem; text-transform: capitalize; }
@media (max-width: 620px) {
  .strategy__header, .strategy__block-head { align-items: stretch; flex-direction: column; }
  .strategy__header :deep(button), .proposal-form__actions :deep(button) { width: 100%; min-height: 44px; }
  .proposal-form :deep(button), .proposal-form :deep(input), .proposal-form :deep(textarea) { min-height: 44px; }
  .proposal-form { grid-template-columns: 1fr; }
  .proposal-form > *, .proposal-form__summary, .proposal-form__wide { grid-column: 1; }
  .proposal-types { grid-template-columns: 1fr; }
}
</style>
