// The candidate-evidence and generated-document data layer, tested where a bug
// would be invisible on screen.
//
// Three things here are load-bearing and asserted from the wire rather than the
// rendered page:
//
//   * A missing profile and a missing document are `null`, not thrown errors —
//     mapped from `candidate_profile_not_found` and `document_not_found`. If that
//     mapping regressed, `useAsyncData` would wrap the error in a `NuxtError` and the
//     page could only say "something went wrong" instead of "finish onboarding" or
//     "no such document".
//   * The cache keys start with the prefixes the writes invalidate (`me:evidence`,
//     `documents`), or a generate call would leave a stale list on screen.
//   * `generate` targets `/resume` vs `/cover-letter` by path and sends the language
//     override in the body — the type is not in the body, and a generator that put it
//     there would 422.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import {
  useDocumentQuery,
  useDocumentsQuery,
  useDownloadDocument,
  useEvidenceActions,
  useEvidenceQuery,
  useGenerateDocument,
} from '~/composables/useDocuments'
import { keysMatching } from '~/composables/useApiQuery'
import { stubFetch } from '../support/http'
import {
  candidateDocument,
  claim,
  documentList,
  evidence,
  evidenceList,
} from '../support/v2-fixtures'

const OPPORTUNITY_ID = '55555555-5555-4555-8555-555555555555'
const DOCUMENT_ID = '88888888-8888-4888-8888-888888888888'

/**
 * Run a composable once, inside a component's `setup`, and return its handle.
 *
 * The component stays mounted for the rest of the test so the query stays in the
 * `useApiQuery` registry (which is what `keysMatching` reads). The composable must
 * run in `setup`, not the render function — a render-function call would re-invoke it
 * on every re-render and fire the fetch twice.
 */
async function run<T>(composable: () => T): Promise<T> {
  let handle!: T
  await mountSuspended(defineComponent({
    setup() {
      handle = composable()
      return () => h('div')
    },
  }))
  return handle
}

describe('useEvidenceQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('reads the attested record and keys it under me:evidence', async () => {
    const http = stubFetch([{ match: '/api/v2/me/evidence', json: evidenceList() }])
    const q = await run(() => useEvidenceQuery())
    await flushPromises()

    expect(http.callsTo('/api/v2/me/evidence')).toHaveLength(1)
    expect(q.data.value?.claims).toHaveLength(1)
    // The prefix the writes invalidate; a mismatch would leave a stale list.
    expect(keysMatching('me:evidence')).toContain('me:evidence')
  })

  it('maps a missing profile to null rather than an error', async () => {
    stubFetch([{
      match: '/api/v2/me/evidence',
      status: 404,
      json: { error: 'candidate_profile_not_found', detail: 'no profile' },
    }])
    const q = await run(() => useEvidenceQuery())
    await flushPromises()

    // `null` is the payload, not the error: the 404 was caught and mapped, so the
    // query resolved rather than surfacing a `NuxtError` the page could not read.
    expect(q.error.value).toBeFalsy()
    expect(q.data.value).toBeNull()
  })
})

describe('useEvidenceActions', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('POSTs an evidence record and a claim to their own endpoints', async () => {
    const http = stubFetch([
      { match: '/api/v2/me/evidence', method: 'POST', status: 201, json: evidence() },
      { match: '/api/v2/me/claims', method: 'POST', status: 201, json: claim() },
    ])
    const actions = await run(() => useEvidenceActions())

    await actions.addEvidence.mutateAsync({
      kind: 'CV_BULLET', provenance: 'MANUAL_USER_INPUT', summary: 'Shipped X',
    })
    await actions.addClaim.mutateAsync({
      claim_type: 'SKILL', label: 'Python',
      evidence_ids: ['66666666-6666-4666-8666-666666666666'],
    })

    expect(http.callsTo('/api/v2/me/evidence').map(c => c.method)).toContain('POST')
    expect(http.callsTo('/api/v2/me/claims')).toHaveLength(1)
    expect(http.bodyOf('/api/v2/me/claims')).toMatchObject({ claim_type: 'SKILL' })
  })
})

describe('useDocumentsQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('reads the document list and keys it under documents', async () => {
    const http = stubFetch([{ match: '/api/v2/documents', json: documentList() }])
    const q = await run(() => useDocumentsQuery())
    await flushPromises()

    expect(http.callsTo('/api/v2/documents')).toHaveLength(1)
    expect(q.data.value?.documents).toHaveLength(1)
    expect(keysMatching('documents')).toContain('documents:list')
  })
})

describe('useDocumentQuery', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('sends nothing while the id is unknown', async () => {
    const http = stubFetch([{ match: '/api/v2/documents/', json: candidateDocument() }])
    await run(() => useDocumentQuery(null))
    await flushPromises()

    expect(http.calls).toHaveLength(0)
  })

  it('fetches the one document once the id is known', async () => {
    const http = stubFetch([{ match: '/api/v2/documents/', json: candidateDocument() }])
    const q = await run(() => useDocumentQuery(DOCUMENT_ID))
    await flushPromises()

    expect(q.data.value?.document_type).toBe('RESUME')
    expect(http.callsTo(`/api/v2/documents/${DOCUMENT_ID}`)).toHaveLength(1)
  })

  it('maps a missing or not-yours document to null', async () => {
    stubFetch([{
      match: '/api/v2/documents/',
      status: 404,
      json: { error: 'document_not_found', detail: 'no such document' },
    }])
    const q = await run(() => useDocumentQuery(DOCUMENT_ID))
    await flushPromises()

    expect(q.error.value).toBeFalsy()
    expect(q.data.value).toBeNull()
  })
})

describe('useGenerateDocument', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('targets /resume and /cover-letter by path, language in the body', async () => {
    const http = stubFetch([
      { match: '/resume', method: 'POST', json: candidateDocument() },
      {
        match: '/cover-letter',
        method: 'POST',
        json: candidateDocument({ document_type: 'COVER_LETTER' }),
      },
    ])
    const gen = await run(() => useGenerateDocument())

    await gen.resume.mutateAsync({ opportunityId: OPPORTUNITY_ID, language: 'fr' })
    await gen.coverLetter.mutateAsync({ opportunityId: OPPORTUNITY_ID })

    expect(http.callsTo(`/opportunities/${OPPORTUNITY_ID}/resume`)).toHaveLength(1)
    expect(http.bodyOf(`/opportunities/${OPPORTUNITY_ID}/resume`))
      .toEqual({ language: 'fr' })
    // No language override sends an explicit null, not an absent field.
    expect(http.bodyOf(`/opportunities/${OPPORTUNITY_ID}/cover-letter`))
      .toEqual({ language: null })
  })

  it('surfaces insufficient_evidence as an ApiError with that code', async () => {
    stubFetch([{
      match: '/resume',
      method: 'POST',
      status: 409,
      json: { error: 'insufficient_evidence', detail: 'no evidence' },
    }])
    const gen = await run(() => useGenerateDocument())

    const error = await gen.resume
      .mutateAsync({ opportunityId: OPPORTUNITY_ID })
      .catch((e: unknown) => e)
    expect((error as { code: string }).code).toBe('insufficient_evidence')
  })
})

describe('useDownloadDocument', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('fetches the PDF and saves it under the disposition filename', async () => {
    const pdf = new Blob([new Uint8Array([0x25, 0x50, 0x44, 0x46])], {
      type: 'application/pdf',
    })
    const http = stubFetch([{
      match: '/download',
      respond: () => new Response(pdf, {
        status: 200,
        headers: {
          'Content-Type': 'application/pdf',
          'Content-Disposition': `attachment; filename="${DOCUMENT_ID}.pdf"`,
        },
      }),
    }])
    // The save is a DOM gesture. Override just the two object-URL methods rather than
    // the whole `URL` global — stubbing the global would break `new URL(...)`, which
    // happy-dom uses internally, and take the rest of the file down with it.
    const createUrl = vi.fn(() => 'blob:fake')
    const revokeUrl = vi.fn()
    const origCreate = URL.createObjectURL
    const origRevoke = URL.revokeObjectURL
    URL.createObjectURL = createUrl
    URL.revokeObjectURL = revokeUrl
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    try {
      const download = await run(() => useDownloadDocument())
      await download.mutateAsync(DOCUMENT_ID)

      expect(http.callsTo(`/api/v2/documents/${DOCUMENT_ID}/download`)).toHaveLength(1)
      expect(createUrl).toHaveBeenCalledOnce()
      expect(click).toHaveBeenCalledOnce()
      // Revoked, not leaked: the blob is pinned in memory until it is.
      expect(revokeUrl).toHaveBeenCalledWith('blob:fake')
    }
    finally {
      URL.createObjectURL = origCreate
      URL.revokeObjectURL = origRevoke
    }
  })

  it('surfaces document_not_rendered without saving anything', async () => {
    stubFetch([{
      match: '/download',
      status: 409,
      json: { error: 'document_not_rendered', detail: 'no rendered version' },
    }])
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    const download = await run(() => useDownloadDocument())

    const error = await download.mutateAsync(DOCUMENT_ID).catch((e: unknown) => e)
    expect((error as { code: string }).code).toBe('document_not_rendered')
    expect(click).not.toHaveBeenCalled()
  })
})
