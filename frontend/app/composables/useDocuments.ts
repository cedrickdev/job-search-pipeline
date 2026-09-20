// The `/api/v2` candidate-evidence and generated-document surface: three reads,
// four writes and a binary download.
//
// A file of its own, next to useAccount.ts rather than folded into it, because the
// two answer to a different rule of the domain even though both are `/me`-scoped.
// The account surface is a profile and its searches — a handful of fields a form
// edits. This is the *truth guarantee* made operable: the evidence store is the set
// of attested facts a document may draw on, and a generated résumé or cover letter
// is only ever a selection and re-ordering of them (docs/CANDIDATE_EVIDENCE.md,
// docs/ATS_DOCUMENTS.md). Keeping it apart keeps that boundary legible.
//
// Three things here are decisions rather than plumbing.
//
// **A missing profile is `null`, not an error** — the same shape useAccount.ts uses
// for `GET /me/profile`. `GET /me/evidence` answers 404 with
// `candidate_profile_not_found` before onboarding has saved a profile, and the
// evidence page is exactly the screen that must tell "no profile yet" apart from "a
// profile with no evidence yet". A missing *document* is `null` too, mapped from
// `document_not_found`, because an id out of a stale link is a screen to render.
//
// **Generation is a `POST`, and it invalidates the whole document surface.** Each
// call is a new attempt that grows the version history, not an idempotent replace,
// so it cannot be a query. A new version changes both the list and that document's
// detail, so the success handler refreshes the `documents` prefix rather than one
// key — the same reason useAccount.ts refreshes `onboarding` after a profile save.
//
// **The download is not JSON.** The rendered PDF comes back as bytes, so it goes
// through `apiDownload` (which keeps the `Blob`) rather than the JSON helpers, and
// the save is triggered here, in the browser, from an object URL. It is a mutation
// with a pending flag so a button can disable while the fetch is in flight and show
// `document_not_rendered` if no version has cleared the guard yet.
import { computed, toValue } from 'vue'
import type { MaybeRefOrGetter } from 'vue'
import type {
  AddClaimRequest,
  AddEvidenceRequest,
  CandidateClaim,
  CandidateDocument,
  CandidateDocumentList,
  CandidateEvidence,
  CandidateEvidenceList,
  GenerateDocumentRequest,
} from '~/types/v2'
import { ApiError, apiDownload, apiGet, apiPost } from '~/utils/api-client'
import { V2_ENDPOINTS, documentUrl, opportunityDocument } from '~/utils/endpoints'
import { invalidate, useApiQuery } from './useApiQuery'
import { useMutation } from './useMutation'

/** The keys every evidence write refreshes. Prefixes, expanded by `invalidate`. */
const EVIDENCE_KEYS = ['me:evidence'] as const

/**
 * This account's attested record — its evidence and its claims — or `null` when no
 * profile has been saved yet.
 *
 * `null` is the pre-onboarding state, mapped from `candidate_profile_not_found`
 * exactly as useAccount.ts maps a missing profile: the evidence page renders "finish
 * onboarding first" rather than an error, and `useAsyncData` would otherwise wrap the
 * thrown error in a `NuxtError` the page could only report as "something went wrong".
 */
export function useEvidenceQuery() {
  return useApiQuery<CandidateEvidenceList | null>('me:evidence', async () => {
    try {
      return await apiGet<CandidateEvidenceList>(V2_ENDPOINTS.evidence)
    }
    catch (error) {
      if (error instanceof ApiError && error.code === 'candidate_profile_not_found') {
        return null
      }
      throw error
    }
  })
}

/**
 * The two writes on the evidence store: record a fact, then assert a claim on it.
 *
 * Grouped like `useSearchProfileActions`, and for the same reason: the evidence page
 * needs both, and one call site with two mutations keeps their `isPending` flags
 * separate — a claim being asserted must not disable the add-evidence form. Both
 * refresh `me:evidence` so a new record or claim shows without a reload; a claim can
 * only cite evidence already stored, so the order the page uses them in is: add
 * evidence, read back its id, then claim.
 */
export function useEvidenceActions() {
  const refresh = () => invalidate(...EVIDENCE_KEYS)
  return {
    addEvidence: useMutation<AddEvidenceRequest, CandidateEvidence>(
      body => apiPost<CandidateEvidence>(V2_ENDPOINTS.evidence, body),
      { onSuccess: refresh },
    ),
    // 422 `claim_cites_unknown_evidence` when a cited id names no stored record; the
    // page surfaces that as a message rather than letting it reject silently.
    addClaim: useMutation<AddClaimRequest, CandidateClaim>(
      body => apiPost<CandidateClaim>(V2_ENDPOINTS.claims, body),
      { onSuccess: refresh },
    ),
  }
}

/** This account's documents, most recently updated first. */
export function useDocumentsQuery() {
  return useApiQuery<CandidateDocumentList>('documents:list', () =>
    apiGet<CandidateDocumentList>(V2_ENDPOINTS.documents))
}

/**
 * One document with its whole version history, or `null` when there is no such
 * document for this account.
 *
 * `null` is mapped from `document_not_found` — the backend answers the same code for
 * "no such document" and "not yours", so this cannot be used to tell another
 * account's document from a deleted one. `enabled` guards the unknown id: a document
 * page reads it from the route, and a request for `/documents/undefined` would be a
 * 422 the user sees as a broken screen.
 */
export function useDocumentQuery(documentId: MaybeRefOrGetter<string | null>) {
  const id = computed(() => toValue(documentId))
  return useApiQuery<CandidateDocument | null>(
    computed(() => `documents:detail:${id.value ?? 'none'}`),
    async () => {
      try {
        return await apiGet<CandidateDocument>(
          documentUrl(V2_ENDPOINTS.document, id.value as string))
      }
      catch (error) {
        if (error instanceof ApiError && error.code === 'document_not_found') return null
        throw error
      }
    },
    { enabled: computed(() => id.value !== null) },
  )
}

/** What a generate call needs: the posting, and an optional language override. */
export interface GenerateArgs {
  opportunityId: string
  language?: GenerateDocumentRequest['language']
}

/**
 * The two document generators, keyed by posting.
 *
 * Two mutations rather than one taking a type, because the type is in the *path*
 * (`/resume` vs `/cover-letter`), not the body — the backend models them as two
 * endpoints, so this does too. Each returns the whole document, so the caller sees
 * the new version in its history, and each refreshes the `documents` prefix so the
 * list and any open detail both pick up the new attempt. `insufficient_evidence`
 * (409) is the honest refusal when the profile carries too little to build from; the
 * page shows it rather than inventing content.
 */
export function useGenerateDocument() {
  const refresh = () => invalidate('documents')
  const body = (args: GenerateArgs): GenerateDocumentRequest => ({
    language: args.language ?? null,
  })
  return {
    resume: useMutation<GenerateArgs, CandidateDocument>(
      args => apiPost<CandidateDocument>(
        opportunityDocument(V2_ENDPOINTS.opportunityResume, args.opportunityId),
        body(args)),
      { onSuccess: refresh },
    ),
    coverLetter: useMutation<GenerateArgs, CandidateDocument>(
      args => apiPost<CandidateDocument>(
        opportunityDocument(V2_ENDPOINTS.opportunityCoverLetter, args.opportunityId),
        body(args)),
      { onSuccess: refresh },
    ),
  }
}

/**
 * Download the newest rendered PDF of a document and hand it to the browser to save.
 *
 * A mutation, not a query: it is an action a button takes, it has a pending state
 * while the bytes stream, and it fails with `document_not_rendered` (409) when the
 * document exists but no version has cleared the guard and been rendered yet — a
 * temporary state the UI reports, not a missing resource. The save itself is a DOM
 * gesture (an object URL clicked and revoked), which is why it lives in the browser
 * here rather than in the transport layer.
 */
export function useDownloadDocument() {
  return useMutation<string, void>(async (documentId) => {
    const file = await apiDownload(
      documentUrl(V2_ENDPOINTS.documentDownload, documentId))
    saveBlob(file.blob, file.filename ?? `${documentId}.pdf`)
  })
}

/**
 * Trigger a browser save of `blob` under `filename`.
 *
 * `createObjectURL` + a synthetic anchor click is the portable way to save bytes the
 * app already holds without a second round-trip. The URL is revoked immediately
 * after: it pins the blob in memory until it is, and leaking one per download would
 * grow unbounded over a session (docs/ATS_DOCUMENTS.md §Download).
 */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.rel = 'noopener'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  }
  finally {
    URL.revokeObjectURL(url)
  }
}
