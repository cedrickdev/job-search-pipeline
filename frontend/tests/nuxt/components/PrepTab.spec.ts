// Ported from webapp/src/components/PrepTab.test.tsx.
//
// The generate cases are the interesting ones. A prep pack that fails the mandate
// gate is NOT persisted server-side, so the response has to be displayed from the
// mutation result rather than re-read from the API — V1 did that with
// `qc.setQueryData`, the port does it with a local override. Both tests below
// would pass with a refetch-on-success implementation only if the server lied
// about persisting, which is exactly the bug they exist to catch.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import PrepTab from '~/components/PrepTab.vue'
import { prep } from '../support/fixtures'
import { stubFetch } from '../support/http'

const PREP = prep({
  notes_md: 'existing notes',
  likely_questions: ['Tell me about a hard project'],
  generated_at: '2026-06-10T10:00:00',
  interviews: [{
    id: 1,
    round_label: 'Phone',
    scheduled_for: '2026-06-20T10:00:00',
    outcome: null,
    notes: null,
    created_at: '2026-06-11T09:00:00',
  }],
})

describe('PrepTab', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('shows notes, likely questions, and interview rows', async () => {
    stubFetch([{ match: '/api/jobs/7/prep', method: 'GET', json: PREP }])
    const wrapper = await mountSuspended(PrepTab, { props: { jobId: 7 } })
    await flushPromises()

    expect(wrapper.get('textarea').element.value).toBe('existing notes')
    expect(wrapper.text()).toMatch(/Tell me about a hard project/)
    expect(wrapper.text()).toMatch(/Phone/)
  })

  it('saves notes (PUT) on blur', async () => {
    const http = stubFetch([
      { match: '/prep/notes', method: 'PUT', json: { ok: true } },
      { match: '/api/jobs/7/prep', method: 'GET', json: PREP },
    ])
    const wrapper = await mountSuspended(PrepTab, { props: { jobId: 7 } })
    await flushPromises()

    const textarea = wrapper.get('textarea')
    await textarea.setValue('new note')
    await textarea.trigger('blur')
    await flushPromises()

    expect(http.callsTo('/api/jobs/7/prep/notes').map(c => c.method)).toEqual(['PUT'])
    expect(http.bodyOf('/prep/notes')).toEqual({ notes_md: 'new note' })
  })

  it('does not PUT when the notes were not edited', async () => {
    // Not a V1 case. The blur handler compares against the loaded value on
    // purpose: opening the tab and clicking away must not write.
    const http = stubFetch([
      { match: '/prep/notes', method: 'PUT', json: { ok: true } },
      { match: '/api/jobs/7/prep', method: 'GET', json: PREP },
    ])
    const wrapper = await mountSuspended(PrepTab, { props: { jobId: 7 } })
    await flushPromises()

    await wrapper.get('textarea').trigger('blur')
    await flushPromises()

    expect(http.called('/prep/notes')).toBe(false)
  })
})

const EMPTY_PREP = prep()

describe('PrepTab generate', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  function routes(generateBody: object) {
    return [
      { match: '/prep/generate', method: 'POST', json: generateBody },
      { match: '/api/jobs/7/prep', method: 'GET', json: EMPTY_PREP },
    ]
  }

  async function clickGenerate(wrapper: VueWrapper) {
    const button = wrapper.findAll('button').find(b => /generate prep/i.test(b.text()))
    if (!button) throw new Error('no "Generate prep" button')
    await button.trigger('click')
    await flushPromises()
  }

  it('generates prep and shows the new questions', async () => {
    stubFetch(routes({
      ...EMPTY_PREP,
      likely_questions: ['Why this team?'],
      generated_at: '2026-06-16T12:00:00',
      mandate_ok: true,
      flags: [],
    }))
    const wrapper = await mountSuspended(PrepTab, { props: { jobId: 7 } })
    await flushPromises()

    await clickGenerate(wrapper)
    expect(wrapper.text()).toContain('Why this team?')
  })

  it('warns when generated prep fails the mandate gate', async () => {
    stubFetch(routes({
      ...EMPTY_PREP,
      likely_questions: ['draft q'],
      generated_at: null,
      mandate_ok: false,
      flags: ['anonymization_config_missing'],
    }))
    const wrapper = await mountSuspended(PrepTab, { props: { jobId: 7 } })
    await flushPromises()

    await clickGenerate(wrapper)
    expect(wrapper.text()).toContain('anonymization_config_missing')
    expect(wrapper.get('[role="alert"]').text()).toMatch(/did not clear the safety gate/i)
    // The draft is still shown — withholding it would leave the user with nothing
    // to fix — but never as saved prep.
    expect(wrapper.text()).toContain('draft q')
  })

  it('drops the unsaved draft once a real write lands', async () => {
    // Not a V1 case, but it is the failure mode the override introduces: the
    // shadowed pack must not outlive the next mutation, or a fail-closed draft
    // would keep masking the persisted prep.
    stubFetch([
      ...routes({ ...EMPTY_PREP, likely_questions: ['draft q'], mandate_ok: false, flags: [] }),
      { match: '/prep/notes', method: 'PUT', json: { ok: true } },
    ])
    const wrapper = await mountSuspended(PrepTab, { props: { jobId: 7 } })
    await flushPromises()

    await clickGenerate(wrapper)
    expect(wrapper.text()).toContain('draft q')

    const textarea = wrapper.get('textarea')
    await textarea.setValue('note')
    await textarea.trigger('blur')
    await flushPromises()

    expect(wrapper.text()).not.toContain('draft q')
  })
})
