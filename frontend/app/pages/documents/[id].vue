<!--
  One document, its whole version history, and why each attempt stands where it does.

  This is where the truth guarantee becomes visible. A version is not just "the PDF" —
  it carries the guard's verdict, and a rejected attempt is kept with the violations
  that sank it (docs/ATS_DOCUMENTS.md §Audit). So the page shows every version, newest
  first, and for a rejected one it names the offending line and the rule it broke,
  because an auditable refusal is the point: the reader can see the platform declined
  to write something the evidence did not support, rather than quietly rewriting it.

  The content shown is the structured document the guard checked, not a preview of a
  different thing — the same summary, bullets and paragraphs, each still tied to the
  evidence it cites. The download streams the newest *rendered* PDF; a document with no
  rendered version has nothing to download yet, and the button says so instead of
  handing back a 409 the user cannot read.

  A missing or not-yours id is `null`, an ordinary "no such document" screen, because
  the backend answers the same 404 for both and a stale link is not a failure to
  report.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useDocumentQuery, useDownloadDocument } from '~/composables/useDocuments'
import type { CandidateDocument, DocumentVersion } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

definePageMeta({ middleware: 'auth' })

const route = useRoute()
const documentId = computed(() => {
  const raw = route.params.id
  const value = Array.isArray(raw) ? raw[0] : raw
  return value ? value : null
})

const { data, status, error } = useDocumentQuery(documentId)
const download = useDownloadDocument()

const doc = computed(() => data.value ?? null)
const loading = computed(() => status.value === 'pending' && data.value === undefined)
// `null`, not an error: the query maps `document_not_found` to it.
const missing = computed(() => !error.value && data.value === null)

// Newest first: the API sends versions oldest-to-newest with strictly increasing
// numbers, and a history reads best with the latest attempt at the top.
const versions = computed<DocumentVersion[]>(() =>
  doc.value ? [...doc.value.versions].reverse() : [])
const hasRendered = computed(() =>
  versions.value.some(version => version.status === 'RENDERED'))
const downloadError = computed(() =>
  (download.error ? errorMessage(download.error) : null))

const TYPE_LABEL: Record<CandidateDocument['document_type'], string> = {
  RESUME: 'Résumé',
  COVER_LETTER: 'Cover letter',
}

useHead({
  title: () => `${doc.value ? TYPE_LABEL[doc.value.document_type] : 'Document'} · Command Center`,
})

function when(iso: string): string {
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? '—' : at.toLocaleString()
}

/** Bytes as a short human size for the artifact line. */
function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const kb = bytes / 1024
  return kb < 1024 ? `${kb.toFixed(0)} KB` : `${(kb / 1024).toFixed(1)} MB`
}

function onDownload() {
  if (documentId.value) download.mutate(documentId.value)
}
</script>

<template>
  <div class="acct-page">
    <p v-if="loading" class="ov-state">
      Loading document…
    </p>
    <p v-else-if="missing" class="ov-state">
      That document does not exist.
      <NuxtLink to="/documents">
        Back to documents
      </NuxtLink>
    </p>
    <p v-else-if="error || !doc" class="ov-state ov-error">
      Could not load this document.
    </p>
    <template v-else>
      <header class="acct-head">
        <h1>{{ TYPE_LABEL[doc.document_type] }}</h1>
        <div class="doc-head-actions">
          <button
            type="button"
            class="btn-primary"
            :disabled="!hasRendered || download.isPending"
            @click="onDownload"
          >
            {{ download.isPending ? 'Preparing…' : 'Download PDF' }}
          </button>
          <NuxtLink to="/documents" class="btn-ghost">
            ← Documents
          </NuxtLink>
        </div>
      </header>

      <p v-if="!hasRendered" class="settings-hint">
        No version has been rendered yet, so there is nothing to download. A version is
        rendered once it clears the evidence guard.
      </p>
      <p v-if="downloadError" class="auth-error" role="alert">
        {{ downloadError }}
      </p>

      <ol class="doc-versions">
        <li v-for="version in versions" :key="version.id" class="settings-card doc-version">
          <div class="acct-search-row">
            <div class="acct-search-main">
              <span class="acct-search-name">Version {{ version.version }}</span>
              <span class="acct-search-meta">
                {{ version.language }} · generated {{ when(version.created_at) }}
                <template v-if="version.generator_key">
                  · <code>{{ version.generator_key }}</code>
                </template>
              </span>
            </div>
            <span
              :class="version.status === 'REJECTED' ? 'acct-paused' : 'acct-live'"
            >
              {{ version.status }}
            </span>
          </div>

          <p v-if="version.artifact" class="acct-search-meta">
            {{ version.artifact.page_count ?? '?' }} page(s) ·
            {{ size(version.artifact.byte_size) }} ·
            rendered {{ when(version.artifact.rendered_at) }}
          </p>

          <!-- A rejected attempt, kept with the reasons it was rejected. -->
          <div
            v-if="version.guard_report && !version.guard_report.ok"
            class="doc-violations"
          >
            <p class="doc-violations-head">
              The guard refused this version:
            </p>
            <ul class="co-evidence">
              <li v-for="(v, i) in version.guard_report.violations" :key="i">
                <code>{{ v.code }}</code> — {{ v.detail }}
                <span v-if="v.offending_text" class="doc-offending">
                  “{{ v.offending_text }}”
                </span>
              </li>
            </ul>
          </div>

          <!-- Résumé content, as the guard checked it. -->
          <div v-if="version.content.kind === 'RESUME'" class="doc-content">
            <p class="doc-name">
              {{ version.content.full_name }}
            </p>
            <p v-if="version.content.headline" class="doc-headline">
              {{ version.content.headline }}
            </p>
            <p v-if="version.content.summary" class="doc-summary">
              {{ version.content.summary.text }}
            </p>
            <section v-if="version.content.experience.length" class="doc-section">
              <h3>Experience</h3>
              <div v-for="(entry, i) in version.content.experience" :key="i" class="doc-entry">
                <p class="doc-entry-heading">
                  {{ entry.heading }}
                  <span v-if="entry.subheading" class="acct-search-meta">
                    — {{ entry.subheading }}
                  </span>
                </p>
                <ul>
                  <li v-for="(bullet, b) in entry.bullets" :key="b">
                    {{ bullet.text }}
                  </li>
                </ul>
              </div>
            </section>
            <section v-if="version.content.education.length" class="doc-section">
              <h3>Education</h3>
              <div v-for="(entry, i) in version.content.education" :key="i" class="doc-entry">
                <p class="doc-entry-heading">
                  {{ entry.heading }}
                  <span v-if="entry.subheading" class="acct-search-meta">
                    — {{ entry.subheading }}
                  </span>
                </p>
              </div>
            </section>
            <section v-if="version.content.skill_groups.length" class="doc-section">
              <h3>Skills</h3>
              <p v-for="(group, i) in version.content.skill_groups" :key="i">
                <strong v-if="group.name">{{ group.name }}:</strong>
                {{ group.skills.join(', ') }}
              </p>
            </section>
            <section v-if="version.content.languages.length" class="doc-section">
              <h3>Languages</h3>
              <p>{{ version.content.languages.join(', ') }}</p>
            </section>
          </div>

          <!-- Cover-letter content. -->
          <div v-else class="doc-content doc-letter">
            <p v-if="version.content.recipient">
              {{ version.content.recipient }}
            </p>
            <p v-if="version.content.greeting">
              {{ version.content.greeting }}
            </p>
            <p v-for="(para, i) in version.content.body" :key="i">
              {{ para.text }}
            </p>
            <p v-if="version.content.closing">
              {{ version.content.closing }}
            </p>
            <p class="doc-signature">
              {{ version.content.signature }}
            </p>
          </div>
        </li>
      </ol>
    </template>
  </div>
</template>

<style scoped>
.doc-head-actions {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}
.doc-versions {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.doc-violations {
  margin-top: 0.5rem;
  padding: 0.5rem 0.75rem;
  border-left: 3px solid var(--color-danger, #b91c1c);
}
.doc-violations-head {
  font-weight: bold;
  margin: 0 0 0.25rem 0;
}
.doc-offending {
  font-style: italic;
  opacity: 0.85;
}
.doc-content {
  margin-top: 0.75rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--color-border, #33415522);
}
.doc-name {
  font-size: 1.15rem;
  font-weight: bold;
  margin: 0;
}
.doc-headline {
  margin: 0 0 0.5rem 0;
  opacity: 0.85;
}
.doc-section {
  margin-top: 0.5rem;
}
.doc-section h3 {
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin: 0.5rem 0 0.25rem 0;
}
.doc-entry-heading {
  font-weight: 600;
  margin: 0.25rem 0 0.1rem 0;
}
.doc-letter p {
  margin: 0 0 0.5rem 0;
}
.doc-signature {
  margin-top: 0.75rem;
  font-weight: 600;
}
</style>
