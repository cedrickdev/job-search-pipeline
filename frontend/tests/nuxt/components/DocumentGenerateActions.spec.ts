// Generating a document from a posting, tested for the two things the button owns
// beyond the composable it wraps (which useDocuments.spec.ts already pins):
//
//   * a successful generation routes to the new document, so the person lands on the
//     version and its guard verdict rather than being left on the map;
//   * `insufficient_evidence` (409) becomes a line pointing at the evidence page — the
//     honest refusal, not a thrown error and never invented content.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import DocumentGenerateActions from '~/components/DocumentGenerateActions.vue'
import { stubFetch } from '../support/http'
import { candidateDocument } from '../support/v2-fixtures'
import type { VueWrapper } from '@vue/test-utils'

const OPPORTUNITY_ID = '55555555-5555-4555-8555-555555555555'
const DOCUMENT_ID = '88888888-8888-4888-8888-888888888888'

function buttonNamed(wrapper: VueWrapper, label: RegExp) {
  const found = wrapper.findAll('button').find(b => label.test(b.text()))
  if (!found) throw new Error(`no button matching ${label}`)
  return found
}

describe('DocumentGenerateActions', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('generates a résumé and routes to the new document', async () => {
    stubFetch([{ match: '/resume', method: 'POST', json: candidateDocument() }])
    const wrapper = await mountSuspended(DocumentGenerateActions, {
      props: { opportunityId: OPPORTUNITY_ID },
    })
    const push = vi.spyOn(useRouter(), 'push').mockResolvedValue()

    await buttonNamed(wrapper, /Résumé/).trigger('click')
    await flushPromises()

    // The generator returns the whole document; the person is taken to read it.
    expect(push).toHaveBeenCalledWith(`/documents/${DOCUMENT_ID}`)
  })

  it('targets the cover-letter endpoint from its own button', async () => {
    const http = stubFetch([{
      match: '/cover-letter',
      method: 'POST',
      json: candidateDocument({ document_type: 'COVER_LETTER' }),
    }])
    const wrapper = await mountSuspended(DocumentGenerateActions, {
      props: { opportunityId: OPPORTUNITY_ID },
    })
    vi.spyOn(useRouter(), 'push').mockResolvedValue()

    await buttonNamed(wrapper, /Cover letter/).trigger('click')
    await flushPromises()

    expect(http.callsTo(`/opportunities/${OPPORTUNITY_ID}/cover-letter`)).toHaveLength(1)
  })

  it('shows the honest refusal and does not navigate on insufficient evidence', async () => {
    stubFetch([{
      match: '/resume',
      method: 'POST',
      status: 409,
      json: { error: 'insufficient_evidence', detail: 'no evidence' },
    }])
    const wrapper = await mountSuspended(DocumentGenerateActions, {
      props: { opportunityId: OPPORTUNITY_ID },
    })
    const push = vi.spyOn(useRouter(), 'push').mockResolvedValue()

    await buttonNamed(wrapper, /Résumé/).trigger('click')
    await flushPromises()

    expect(wrapper.get('.doc-actions-error').text())
      .toContain('There is not enough evidence on your profile')
    // Points at the fix — add evidence — rather than a dead end.
    expect(wrapper.get('.doc-actions-error a').attributes('href')).toBe('/evidence')
    expect(push).not.toHaveBeenCalled()
  })
})
