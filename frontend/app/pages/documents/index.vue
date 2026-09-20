<!--
  The documents list: every résumé and cover letter this account has generated.

  A document is one posting's résumé or cover letter across its attempts — each
  generation appends a version, and `latest_usable_version` names the newest one fit
  to be shown as the candidate's (docs/ATS_DOCUMENTS.md). This screen reads only; a
  document is created from a posting (the map and jobs screens are where a generation
  starts), so there is no "new document" button here — there is nothing to generate a
  document *about* on this page.

  What each row states plainly: the type, the posting it targets, how many attempts
  there have been, and whether any has cleared the guard and been rendered. A document
  with no usable version is not a failure to hide — it is either still being worked or
  every attempt was rejected for citing something the evidence does not support, and
  the row says which.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useDocumentsQuery } from '~/composables/useDocuments'
import type { CandidateDocument } from '~/types/v2'

definePageMeta({ middleware: 'auth' })

useHead({ title: 'Documents · Command Center' })

const { data, status, error } = useDocumentsQuery()

const loading = computed(() => status.value === 'pending' && !data.value)
const documents = computed(() => data.value?.documents ?? [])

/** A date as the reader's locale writes it; the API sends UTC ISO-8601. */
function when(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? '—' : at.toLocaleString()
}

/** Whether a document has a version fit to be shown as the candidate's. */
function isUsable(doc: CandidateDocument): boolean {
  return doc.latest_usable_version !== null
}

const TYPE_LABEL: Record<CandidateDocument['document_type'], string> = {
  RESUME: 'Résumé',
  COVER_LETTER: 'Cover letter',
}
</script>

<template>
  <div class="acct-page">
    <header class="acct-head">
      <h1>Documents</h1>
      <NuxtLink to="/evidence" class="btn-ghost">
        Evidence →
      </NuxtLink>
    </header>

    <p class="settings-hint">
      Résumés and cover letters generated for the postings you have looked at. Each is
      built only from your recorded evidence, and every attempt is kept — including the
      ones the guard refused.
    </p>

    <p v-if="loading" class="ov-state">
      Loading documents…
    </p>
    <p v-else-if="error" class="ov-state ov-error">
      Could not load your documents.
    </p>
    <template v-else>
      <p v-if="!documents.length" class="ov-empty">
        No documents yet. Open a posting to generate a résumé or a cover letter.
      </p>
      <ul v-else class="acct-searches">
        <li v-for="doc in documents" :key="doc.id" class="acct-search">
          <div class="acct-search-row">
            <div class="acct-search-main">
              <NuxtLink :to="`/documents/${doc.id}`" class="acct-search-name">
                {{ TYPE_LABEL[doc.document_type] }}
              </NuxtLink>
              <span class="acct-search-meta">
                {{ doc.versions.length }}
                {{ doc.versions.length === 1 ? 'attempt' : 'attempts' }} ·
                updated {{ when(doc.updated_at) }}
              </span>
            </div>
            <span :class="isUsable(doc) ? 'acct-live' : 'acct-paused'">
              {{ isUsable(doc) ? `v${doc.latest_usable_version} ready` : 'no usable version' }}
            </span>
          </div>
        </li>
      </ul>
    </template>
  </div>
</template>
