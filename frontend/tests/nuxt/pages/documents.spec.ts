// The document screens: the list, and one document with its version history.
//
// Two Phase 10 requirements are asserted the way a user meets them.
//
// **A rejected attempt is kept, with why it was rejected.** The whole point of the
// guard is auditable (§45): the detail page shows a REJECTED version and names the
// rule it broke and the line that broke it, rather than hiding a refusal. A screen
// that dropped rejected versions would erase the record the guarantee is made of.
//
// **A document with no rendered version cannot be downloaded, and says so.** The
// download button is disabled and the page states the reason, rather than offering a
// download that would 409. The detail page tells "no such document" (a stale link,
// mapped from `document_not_found`) apart from a real error.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import DocumentsPage from '~/pages/documents/index.vue'
import DocumentPage from '~/pages/documents/[id].vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import {
  candidateDocument,
  documentList,
  documentVersion,
  signedIn,
} from '../support/v2-fixtures'
import type { Route } from '../support/http'
import type { CandidateDocument, CandidateDocumentList } from '~/types/v2'
import type { VueWrapper } from '@vue/test-utils'

const DOCUMENT_ID = '88888888-8888-4888-8888-888888888888'
const LIST = '/api/v2/documents'

function listRoutes(list: CandidateDocumentList = documentList()): Route[] {
  return [
    { match: LIST, method: 'GET', json: list },
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  ]
}

function detailRoutes(detail: CandidateDocument = candidateDocument(),
                      extra: Route[] = []): Route[] {
  return [
    ...extra,
    { match: `/api/v2/documents/${DOCUMENT_ID}`, method: 'GET', json: detail },
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  ]
}

async function signIn(routeTable: Route[]) {
  const http = stubFetch(routeTable)
  await useSessionStore().ensure()
  return http
}

async function mountList(list?: CandidateDocumentList) {
  const http = await signIn(listRoutes(list))
  const wrapper = await mountSuspended(DocumentsPage, { route: '/documents' })
  await flushPromises()
  return { http, wrapper }
}

async function mountDetail(detail?: CandidateDocument, extra: Route[] = []) {
  const http = await signIn(detailRoutes(detail, extra))
  const wrapper = await mountSuspended(DocumentPage, { route: `/documents/${DOCUMENT_ID}` })
  await flushPromises()
  return { http, wrapper }
}

function buttonNamed(wrapper: VueWrapper, label: RegExp) {
  const found = wrapper.findAll('button').find(b => label.test(b.text()))
  if (!found) throw new Error(`no button matching ${label}`)
  return found
}

describe('documents page · the list', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    useSessionStore().$reset()
  })

  it('lists each document with its type and whether a version is usable', async () => {
    const { wrapper } = await mountList()

    expect(wrapper.text()).toContain('Résumé')
    expect(wrapper.text()).toContain('v1 ready')
    expect(wrapper.get('.acct-search-name').attributes('href'))
      .toBe(`/documents/${DOCUMENT_ID}`)
  })

  it('says a document with no usable version has none, rather than hiding it', async () => {
    const draftOnly = candidateDocument({
      latest_usable_version: null,
      versions: [documentVersion({ status: 'DRAFT', artifact: null, guard_report: null })],
    })
    const { wrapper } = await mountList(documentList([draftOnly]))

    expect(wrapper.text()).toContain('no usable version')
  })

  it('says so when there are no documents at all', async () => {
    const { wrapper } = await mountList(documentList([]))

    expect(wrapper.get('.ov-empty').text()).toContain('No documents yet')
  })
})

describe('document page · one document', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    useSessionStore().$reset()
  })

  it('renders the résumé content the guard checked', async () => {
    const { wrapper } = await mountDetail()

    expect(wrapper.get('h1').text()).toBe('Résumé')
    expect(wrapper.text()).toContain('Backend engineer with 8 years of experience')
    expect(wrapper.text()).toContain('Rebuilt the checkout flow, cutting latency 30%')
    expect(buttonNamed(wrapper, /Download PDF/).attributes('disabled')).toBeUndefined()
  })

  it('keeps a rejected attempt on screen with the rule it broke', async () => {
    const rejected = candidateDocument({
      latest_usable_version: null,
      versions: [documentVersion({
        status: 'REJECTED',
        artifact: null,
        guard_report: {
          ok: false,
          violations: [{
            code: 'INVENTED_NUMBER',
            detail: 'a metric no evidence supports',
            evidence_ids: [],
            offending_text: 'grew revenue 300%',
          }],
        },
      })],
    })
    const { wrapper } = await mountDetail(rejected)

    expect(wrapper.text()).toContain('REJECTED')
    expect(wrapper.text()).toContain('INVENTED_NUMBER')
    expect(wrapper.text()).toContain('a metric no evidence supports')
    // The offending line is quoted, so a reviewer sees the sentence, not a section number.
    expect(wrapper.text()).toContain('grew revenue 300%')
  })

  it('cannot offer a download when nothing has been rendered', async () => {
    const notRendered = candidateDocument({
      latest_usable_version: null,
      versions: [documentVersion({ status: 'VALIDATED', artifact: null })],
    })
    const { wrapper } = await mountDetail(notRendered)

    expect(buttonNamed(wrapper, /Download PDF/).attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('nothing to download')
  })

  it('downloads the PDF through the document id', async () => {
    const pdf = new Blob([new Uint8Array([0x25, 0x50, 0x44, 0x46])], { type: 'application/pdf' })
    const createUrl = vi.fn(() => 'blob:fake')
    const revokeUrl = vi.fn()
    const origCreate = URL.createObjectURL
    const origRevoke = URL.revokeObjectURL
    URL.createObjectURL = createUrl
    URL.revokeObjectURL = revokeUrl
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

    try {
      const { http, wrapper } = await mountDetail(candidateDocument(), [{
        match: '/download',
        respond: () => new Response(pdf, {
          status: 200,
          headers: {
            'Content-Type': 'application/pdf',
            'Content-Disposition': `attachment; filename="${DOCUMENT_ID}.pdf"`,
          },
        }),
      }])

      await buttonNamed(wrapper, /Download PDF/).trigger('click')
      await flushPromises()

      expect(http.callsTo(`/api/v2/documents/${DOCUMENT_ID}/download`)).toHaveLength(1)
      expect(createUrl).toHaveBeenCalledOnce()
    }
    finally {
      URL.createObjectURL = origCreate
      URL.revokeObjectURL = origRevoke
    }
  })

  it('says no such document on a 404 rather than an error', async () => {
    const { wrapper } = await mountDetail(candidateDocument(), [{
      match: `/api/v2/documents/${DOCUMENT_ID}`,
      method: 'GET',
      status: 404,
      json: { error: 'document_not_found', detail: 'no such document' },
    }])

    expect(wrapper.text()).toContain('That document does not exist')
    expect(wrapper.find('.ov-error').exists()).toBe(false)
  })
})
