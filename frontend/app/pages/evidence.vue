<!--
  The candidate evidence store: the attested facts a document may be built from.

  This screen is the write side of the truth guarantee. Everything a generated résumé
  or cover letter can say has to trace to a record here — the platform may reorder,
  shorten or omit these facts, but it may never invent one that is not on this page
  (docs/CANDIDATE_EVIDENCE.md). So the two forms below are not "profile fields": they
  are the act of attesting a fact and then asserting a claim that rests on it.

  Two things are deliberate.

  **A claim cites evidence, and only evidence already stored.** The claim form's
  evidence picker lists the records above it, because a claim resting on nothing is
  unconstructible and the backend refuses one citing an id it does not hold
  (`claim_cites_unknown_evidence`). Recording a fact and claiming something about it
  are two steps in that order, which is why they are two forms.

  **A missing profile is its own state, not an error.** Evidence hangs off a profile,
  so before onboarding has saved one there is nothing to hang it on; the screen says
  so and points at onboarding rather than rendering forms that would 404.
-->
<script setup lang="ts">
import { computed, reactive } from 'vue'
import { useEvidenceActions, useEvidenceQuery } from '~/composables/useDocuments'
import type {
  AddClaimRequest,
  AddEvidenceRequest,
  CandidateClaim,
  ClaimType,
  EvidenceKind,
  EvidenceProvenance,
} from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'
import { useLocale } from '~/composables/useLocale'

definePageMeta({ middleware: 'auth' })

// The closed sets the two forms offer, in the order the enums declare them. Manual
// entry defaults to what a person typing here actually is: a self-declared fact from
// manual input — never an `LLM_GENERATED` provenance, which does not exist by design.
const KINDS: EvidenceKind[] = [
  'CV_BULLET', 'CV_SUMMARY', 'EMPLOYMENT_RECORD', 'DIPLOMA', 'CERTIFICATE',
  'LANGUAGE_ASSESSMENT', 'PORTFOLIO_ITEM', 'REFERENCE', 'PERMIT_DOCUMENT',
  'SELF_DECLARATION',
]
const PROVENANCES: EvidenceProvenance[] = [
  'MANUAL_USER_INPUT', 'CANDIDATE_PROFILE', 'BASE_CV', 'IMPORTED_CV', 'PROJECT',
  'EMPLOYMENT_RECORD', 'EDUCATION_RECORD', 'SYSTEM_DERIVED',
]
const CLAIM_TYPES: ClaimType[] = [
  'SKILL', 'EXPERIENCE', 'EDUCATION', 'CERTIFICATION', 'LANGUAGE', 'AVAILABILITY',
  'WORK_AUTHORIZATION', 'ACHIEVEMENT',
]

const { data, status } = useEvidenceQuery()
const { locale, t } = useLocale()
const { addEvidence, addClaim } = useEvidenceActions()
useHead({ title: () => `${t('page.evidence')} · Command Center` })

const loading = computed(() => status.value === 'pending' && data.value === undefined)
// `null` is the payload, not a miss: the query maps `candidate_profile_not_found` to
// it, and that is the "finish onboarding first" state rather than a failure.
const noProfile = computed(() => data.value === null)
const evidence = computed(() => data.value?.evidence ?? [])
const claims = computed(() => data.value?.claims ?? [])

const evidenceForm = reactive({
  kind: 'CV_BULLET' as EvidenceKind,
  provenance: 'MANUAL_USER_INPUT' as EvidenceProvenance,
  summary: '',
  reference_key: '',
  detail: '',
})
const claimForm = reactive({
  claim_type: 'SKILL' as ClaimType,
  label: '',
  detail: '',
  evidence_ids: [] as string[],
})

const evidenceError = computed(() =>
  (addEvidence.error ? errorMessage(addEvidence.error) : null))
const claimError = computed(() => (addClaim.error ? errorMessage(addClaim.error) : null))

/** A stored summary looked up by id, so a claim can show what it rests on. */
function evidenceSummary(id: string): string {
  return evidence.value.find(item => item.id === id)?.summary ?? id
}

function citedSummaries(entry: CandidateClaim): string {
  return entry.evidence_ids.map(evidenceSummary).join('; ')
}

function enumLabel(value: string): string {
  return t(`evidence.enum.${value}`, value.replace(/_/g, ' ').toLowerCase())
}

const evidenceKindItems = computed(() => KINDS.map(value => ({ value, label: enumLabel(value) })))
const provenanceItems = computed(() => PROVENANCES.map(value => ({ value, label: enumLabel(value) })))
const claimTypeItems = computed(() => CLAIM_TYPES.map(value => ({ value, label: enumLabel(value) })))
const citedEvidenceItems = computed(() => evidence.value.map(item => ({
  value: item.id,
  label: item.summary,
  description: [enumLabel(item.kind), enumLabel(item.provenance)].join(' · '),
})))

async function onAddEvidence() {
  // Empty strings become the absence of the field, which is what the API means by
  // "not given"; a blank `reference_key` or `detail` is `min_length=1` on the backend
  // and would 422 if sent as "".
  const body: AddEvidenceRequest = {
    kind: evidenceForm.kind,
    provenance: evidenceForm.provenance,
    summary: evidenceForm.summary.trim(),
    reference_key: evidenceForm.reference_key.trim() || null,
    detail: evidenceForm.detail.trim() || null,
  }
  try {
    await addEvidence.mutateAsync(body)
    evidenceForm.summary = ''
    evidenceForm.reference_key = ''
    evidenceForm.detail = ''
  }
  catch {
    // Left as typed, the error rendered below the form.
  }
}

async function onAddClaim() {
  const body: AddClaimRequest = {
    claim_type: claimForm.claim_type,
    label: claimForm.label.trim(),
    evidence_ids: claimForm.evidence_ids as AddClaimRequest['evidence_ids'],
    detail: claimForm.detail.trim() || null,
  }
  try {
    await addClaim.mutateAsync(body)
    claimForm.label = ''
    claimForm.detail = ''
    claimForm.evidence_ids = []
  }
  catch {
    // Left as typed.
  }
}
</script>

<template>
  <div class="acct-page">
    <header class="acct-head">
      <h1>{{ t('page.evidence') }}</h1>
      <NuxtLink to="/documents" class="btn-ghost">
        {{ t('evidence.documentsLink') }}
      </NuxtLink>
    </header>

    <p class="settings-hint">{{ t('evidence.intro') }}</p>

    <p v-if="loading" class="ov-state">
      Loading…
    </p>

    <section v-else-if="noProfile" class="settings-card">
      <h2>{{ t('evidence.noProfile') }}</h2>
      <p class="auth-hint">{{ t('evidence.noProfileHelp') }}</p>
      <NuxtLink to="/onboarding" class="btn-primary">
        {{ t('evidence.goOnboarding') }}
      </NuxtLink>
    </section>

    <template v-else>
      <section class="settings-card">
        <h2>{{ t('evidence.recorded') }}</h2>
        <p v-if="!evidence.length" class="ov-empty">
          {{ t('evidence.empty') }}
        </p>
        <ul v-else class="acct-searches">
          <li v-for="item in evidence" :key="item.id" class="acct-search">
            <div class="acct-search-row">
              <div class="acct-search-main">
                <span class="acct-search-name">{{ item.summary }}</span>
                <span class="acct-search-meta">
                  {{ enumLabel(item.kind) }} · {{ enumLabel(item.provenance) }}
                  <template v-if="item.reference_key">
                    · <code>{{ item.reference_key }}</code>
                  </template>
                </span>
              </div>
            </div>
            <p v-if="item.detail" class="acct-search-meta">
              {{ item.detail }}
            </p>
          </li>
        </ul>
      </section>

      <section class="settings-card">
        <h2>{{ t('evidence.claimsTitle') }}</h2>
        <p class="auth-hint">{{ t('evidence.claimHelp') }}</p>
        <p v-if="!claims.length" class="ov-empty">
          {{ t('evidence.claimEmpty') }}
        </p>
        <ul v-else class="acct-searches">
          <li v-for="entry in claims" :key="entry.id" class="acct-search">
            <div class="acct-search-row">
              <div class="acct-search-main">
                <span class="acct-search-name">{{ entry.label }}</span>
                <span class="acct-search-meta">{{ enumLabel(entry.claim_type) }}</span>
              </div>
            </div>
            <p class="acct-search-meta">
              {{ locale === 'fr' ? 'S’appuie sur :' : 'rests on:' }} {{ citedSummaries(entry) }}
            </p>
          </li>
        </ul>
      </section>

      <section class="settings-card">
        <h2>{{ t('evidence.recordFact') }}</h2>
        <form class="acct-form evidence-grid" @submit.prevent="onAddEvidence">
          <label class="acct-field">
            {{ t('evidence.kind') }}
            <USelect
              v-model="evidenceForm.kind"
              :items="evidenceKindItems"
              value-key="value"
              :aria-label="t('evidence.kindAria')"
            />
          </label>
          <label class="acct-field">
            {{ t('evidence.provenance') }}
            <USelect
              v-model="evidenceForm.provenance"
              :items="provenanceItems"
              value-key="value"
              :aria-label="t('evidence.provenanceAria')"
            />
          </label>
          <label class="acct-field acct-field-wide">
            {{ t('evidence.summaryField') }}
            <input
              v-model="evidenceForm.summary"
              type="text"
              required
              :aria-label="t('evidence.summaryAria')"
              :placeholder="locale === 'fr' ? 'Refonte du paiement : temps de réponse réduit de 30 %' : 'Rebuilt the checkout flow, cutting latency 30%'"
            >
          </label>
          <label class="acct-field">
            {{ t('evidence.referenceKey') }} ({{ t('auth.optional') }})
            <input
              v-model="evidenceForm.reference_key"
              type="text"
              :aria-label="t('evidence.referenceAria')"
              placeholder="acme-checkout"
            >
          </label>
          <label class="acct-field acct-field-wide">
            {{ t('evidence.detail') }} ({{ t('auth.optional') }})
            <input v-model="evidenceForm.detail" type="text" :aria-label="t('evidence.detailAria')">
          </label>
          <button
            type="submit"
            class="btn-primary"
            :disabled="addEvidence.isPending || !evidenceForm.summary.trim()"
          >
            {{ addEvidence.isPending ? t('common.loading') : t('evidence.add') }}
          </button>
        </form>
        <p v-if="evidenceError" class="auth-error" role="alert">
          {{ evidenceError }}
        </p>
      </section>

      <section class="settings-card">
        <h2>{{ t('evidence.claim') }}</h2>
        <form class="acct-form claim-grid" @submit.prevent="onAddClaim">
          <label class="acct-field">
            {{ t('evidence.type') }}
            <USelect
              v-model="claimForm.claim_type"
              :items="claimTypeItems"
              value-key="value"
              :aria-label="t('evidence.typeAria')"
            />
          </label>
          <label class="acct-field acct-field-wide">
            Label
            <input
              v-model="claimForm.label"
              type="text"
              required
              :aria-label="t('evidence.labelAria')"
              placeholder="Senior Backend Engineer"
            >
          </label>
          <fieldset class="acct-field acct-field-wide">
            <legend>{{ t('evidence.cited') }}</legend>
            <p v-if="!evidence.length" class="auth-hint">
              {{ t('evidence.recordFirst') }}
            </p>
            <USelectMenu
              v-else
              v-model="claimForm.evidence_ids"
              data-testid="claim-evidence-picker"
              :items="citedEvidenceItems"
              value-key="value"
              multiple
              searchable
              :placeholder="locale === 'fr' ? 'Sélectionner les preuves à citer' : 'Select evidence to cite'"
              :aria-label="t('evidence.cited')"
            />
          </fieldset>
          <button
            type="submit"
            class="btn-primary"
            :disabled="addClaim.isPending || !claimForm.label.trim() || !claimForm.evidence_ids.length"
          >
            {{ addClaim.isPending ? t('evidence.asserting') : t('evidence.assertClaim') }}
          </button>
        </form>
        <p v-if="claimError" class="auth-error" role="alert">
          {{ claimError }}
        </p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.acct-form {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
  align-items: stretch;
}
.evidence-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: end;
}
.evidence-grid .acct-field-wide,
.evidence-grid button,
.claim-grid .acct-field-wide,
.claim-grid button {
  grid-column: 1 / -1;
}
.acct-check {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  font-weight: normal;
}
@media (max-width: 640px) {
  .evidence-grid { grid-template-columns: 1fr; }
  .evidence-grid .acct-field-wide,
  .evidence-grid button { grid-column: auto; }
}
</style>
