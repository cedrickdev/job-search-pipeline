<script setup lang="ts">
import { computed, ref } from 'vue'
import { useReferenceCvImport } from '~/composables/useDocuments'
import { errorMessage } from '~/utils/v2-errors'
import { useLocale } from '~/composables/useLocale'

const { localize: l } = useLocale()
const purposes = [
  ['GENERAL', 'General', 'Général'], ['JOB', 'Job', 'Emploi'],
  ['INTERNSHIP', 'Internship', 'Stage'], ['APPRENTICESHIP', 'Apprenticeship', 'Apprentissage'],
  ['WORK_STUDY', 'Work-study', 'Alternance'], ['TRAINING', 'Training', 'Formation'],
] as const
type ReferenceCvPurpose = (typeof purposes)[number][0]

const purpose = ref<ReferenceCvPurpose>('GENERAL')
const file = ref<File | null>(null)
const importCv = useReferenceCvImport()
const toast = useToast()
const purposeItems = computed(() => purposes.map(([value, english, french]) => ({
  value,
  label: l(english, french),
})))

function selectFile(event: Event) {
  file.value = (event.target as HTMLInputElement).files?.[0] ?? null
}

async function submit() {
  if (!file.value) return
  try {
    await importCv.mutateAsync({ purpose: purpose.value, file: file.value })
    toast.add({
      title: l('Reference CV imported', 'CV de référence importé'),
      description: l(
        'It can now be used as grounded evidence for tailored documents.',
        'Il peut maintenant servir de preuve pour vos documents adaptés.',
      ),
      color: 'success',
      icon: 'i-lucide-circle-check',
    })
    file.value = null
  }
  catch (error) {
    toast.add({
      title: l('Import failed', 'Import impossible'),
      description: errorMessage(error),
      color: 'error',
      icon: 'i-lucide-circle-alert',
    })
  }
}
</script>

<template>
  <section class="settings-card reference-cv">
    <h2>{{ l('Reference CV', 'CV de référence') }}</h2>
    <p class="auth-hint">
      {{ l('Import a private PDF, DOCX or TXT CV. Its readable content becomes candidate evidence for truthful tailored documents.', 'Importez un CV privé en PDF, DOCX ou TXT. Son contenu lisible devient une preuve candidat pour générer des documents fidèles.') }}
    </p>
    <div class="acct-row">
      <label class="acct-field">
        {{ l('Use case', 'Usage') }}
        <USelect v-model="purpose" data-testid="reference-cv-purpose" :items="purposeItems" value-key="value" />
      </label>
      <div class="acct-field reference-cv__file-field">
        <span class="reference-cv__field-label">{{ l('File', 'Fichier') }}</span>
        <span id="reference-cv-file-help" class="reference-cv__file-help">
          {{ l('PDF, DOCX or TXT, up to 10 MB.', 'PDF, DOCX ou TXT, jusqu’à 10 Mo.') }}
        </span>
        <label class="reference-cv__file-picker" for="reference-cv-file">
          <UIcon name="i-lucide-upload" aria-hidden="true" />
          {{ l('Choose file', 'Choisir un fichier') }}
        </label>
        <input
          id="reference-cv-file"
          class="reference-cv__file-input"
          type="file"
          accept=".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain"
          aria-describedby="reference-cv-file-help"
          @change="selectFile"
        >
        <span class="reference-cv__file-name" aria-live="polite">
          {{ file?.name ?? l('No file selected', 'Aucun fichier sélectionné') }}
        </span>
      </div>
    </div>
    <button class="btn-primary" type="button" :disabled="!file || importCv.isPending" @click="submit">
      {{ importCv.isPending ? l('Importing…', 'Importation…') : l('Import reference CV', 'Importer le CV de référence') }}
    </button>
  </section>
</template>

<style scoped>
.reference-cv__file-field {
  align-items: flex-start;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.reference-cv__field-label {
  font-weight: 600;
}

.reference-cv__file-help,
.reference-cv__file-name {
  color: var(--text-dim, #667085);
  font-size: 0.82rem;
}

.reference-cv__file-picker {
  align-items: center;
  background: var(--surface-2, #f4f6f8);
  border: 1px solid var(--border, #cbd5e1);
  border-radius: var(--radius-sm, 0.45rem);
  color: var(--text, #172033);
  cursor: pointer;
  display: inline-flex;
  font-weight: 600;
  gap: 0.45rem;
  min-height: 2.75rem;
  padding: 0.6rem 0.9rem;
}

.reference-cv__file-picker:hover {
  background: var(--surface-3, #e8edf2);
}

.reference-cv__file-picker:focus-within,
.reference-cv__file-input:focus-visible + .reference-cv__file-name {
  outline: 3px solid color-mix(in srgb, var(--accent, #2563eb) 35%, transparent);
  outline-offset: 2px;
}

.reference-cv__file-input {
  height: 1px;
  opacity: 0;
  position: absolute;
  width: 1px;
}

.reference-cv__file-input:focus-visible {
  height: 2px;
  opacity: 1;
  width: 2px;
}

@media (max-width: 480px) {
  .reference-cv__file-picker { width: 100%; justify-content: center; }
}
</style>
