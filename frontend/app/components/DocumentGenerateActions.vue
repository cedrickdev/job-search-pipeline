<!--
  Generate a résumé or a cover letter for one posting, from the map card.

  This is where a document is *created*: the documents list is read-only because a
  document is always about a posting, and this is the posting in hand. The two buttons
  are the two generators (docs/ATS_DOCUMENTS.md) — each POSTs to the opportunity, grows
  that document's version history, and on success routes to the document so the person
  sees what was built and can read the guard's verdict.

  Language is left to the backend on purpose. It writes the document in the posting's
  own language, falling back to the candidate's first declared one, and never guesses a
  language the candidate did not state (GenerateDocumentRequest) — so this sends no
  override rather than pretending to know better from the browser.

  The honest refusal shows here. `insufficient_evidence` (409) is what the service
  answers when the profile carries too little to build from; it becomes a line under
  the buttons pointing at the evidence page, not a thrown error — the point of the
  guarantee is that the platform declines rather than invents.

  Self-contained by design: it owns its own pending and error state so the map card
  stays presentational, and each generator's `isPending` is separate so asking for a
  résumé does not disable the cover-letter button.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useGenerateDocument } from '~/composables/useDocuments'
import type { CandidateDocument } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

const { localize: globalLocalize } = useLocale()
const props = defineProps<{ opportunityId: string, locale?: 'en' | 'fr' }>()
const l = (english: string, french: string) => props.locale === 'fr' ? french :
  (props.locale === 'en' ? english : globalLocalize(english, french))

const router = useRouter()
const { resume, coverLetter } = useGenerateDocument()

// Either generator's error, whichever last ran; both surface the same way.
const latestError = computed(() => resume.error ?? coverLetter.error)

const isModelError = computed(() => {
  const err = latestError.value
  if (!err || typeof err !== 'object' || !('code' in err)) return false
  return typeof err.code === 'string' && (err.code.startsWith('provider_')
    || ['no_eligible_provider', 'capability_not_supported', 'structured_output_invalid'].includes(err.code))
})

function isQuotaError(error: unknown): error is { code: string } {
  return typeof error === 'object' && error !== null && 'code' in error &&
    (error as { code?: unknown }).code === 'quota_exceeded'
}

const error = computed(() => {
  const err = latestError.value
  if (!err) return null
  if (isQuotaError(err)) {
    return l(
      'Document generation is temporarily unavailable for this plan. Check your usage to continue.',
      'La génération de document est temporairement indisponible pour ce forfait. Consultez votre utilisation pour continuer.',
    )
  }
  return errorMessage(err)
})

async function generate(which: 'resume' | 'coverLetter') {
  const mutation = which === 'resume' ? resume : coverLetter
  try {
    const doc = await mutation.mutateAsync({ opportunityId: props.opportunityId })
    // The generator returns the whole document; go read it and its guard verdict.
    await router.push(`/documents/${(doc as CandidateDocument).id}`)
  }
  catch {
    // Left as the typed error rendered below; a failed generation is not a navigation.
  }
}
</script>

<template>
  <div class="doc-actions">
    <p class="doc-actions-label">
      {{ l('Build from your evidence', 'Créer à partir de vos preuves') }}
    </p>
    <div class="doc-actions-row">
      <button
        type="button"
        class="btn-ghost"
        :disabled="resume.isPending"
        @click="generate('resume')"
      >
        {{ resume.isPending ? l('Generating…', 'Génération…') : l('Resume', 'CV adapté') }}
      </button>
      <button
        type="button"
        class="btn-ghost"
        :disabled="coverLetter.isPending"
        @click="generate('coverLetter')"
      >
        {{ coverLetter.isPending ? l('Generating…', 'Génération…') : l('Cover letter', 'Lettre de motivation') }}
      </button>
    </div>
    <p v-if="error" class="doc-actions-error" role="alert">
      {{ error }}
      <NuxtLink :to="isQuotaError(latestError) ? '/billing' : isModelError ? '/settings#llm' : '/evidence'">
        {{ isQuotaError(latestError)
          ? l('View usage →', 'Voir l’utilisation →')
          : isModelError ? l('Check model connection →', 'Vérifier la connexion au modèle →')
          : l('Review your evidence →', 'Vérifier vos preuves →') }}
      </NuxtLink>
    </p>
  </div>
</template>

<style scoped>
.doc-actions {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  border-top: 1px solid var(--border);
  padding-top: var(--space-2);
  margin-top: var(--space-1);
}
.doc-actions-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  margin: 0;
}
.doc-actions-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.doc-actions-row button {
  font-size: 13px;
  min-height: 40px;
}
@media (max-width: 440px) { .doc-actions-row button { flex: 1 1 100%; min-height: 44px; } }
.doc-actions-error {
  font-size: 12px;
  color: var(--danger, #b91c1c);
  margin: 0;
}
</style>
