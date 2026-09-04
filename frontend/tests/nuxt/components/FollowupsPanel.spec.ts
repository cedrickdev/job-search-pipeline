// Ported from webapp/src/components/FollowupsPanel.test.tsx.
//
// V1 had one component; the Vue port splits the list (FollowupsPanel) from the row
// (FollowupRow) so each row owns its own mutations. The panel is still what gets
// mounted here, so the five V1 cases apply unchanged and the split stays an
// implementation detail rather than a change in what the tests cover.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import FollowupsPanel from '~/components/FollowupsPanel.vue'
import { followup } from '../support/fixtures'
import { stubFetch } from '../support/http'

const ITEMS = [
  followup(),
  followup({
    kind: 'recruiter_no_outbound',
    application_id: 4,
    job_id: 104,
    company: 'Globex',
    title: 'Shift Lead',
    days: 3,
    since: '2026-06-13',
  }),
]

const DRAFT = {
  subject: 'Follow-up: Analyst application at Initech',
  body: 'Dear Hiring Team,\n\nbackground in retail, customer service and weekend shifts.',
  language: 'en',
  mandate_ok: true,
  flags: [],
}

function buttonNamed(wrapper: VueWrapper, label: RegExp) {
  const found = wrapper.findAll('button').find(b => label.test(b.text()))
  if (!found) throw new Error(`no button matching ${label}`)
  return found
}

describe('FollowupsPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('lists each due follow-up with its reason', async () => {
    stubFetch([])
    const wrapper = await mountSuspended(FollowupsPanel, { props: { items: ITEMS } })

    expect(wrapper.text()).toContain('Follow-ups due')
    expect(wrapper.text()).toContain('Initech')
    expect(wrapper.text()).toContain('Globex')
    expect(wrapper.text()).toMatch(/no reply/)
    expect(wrapper.text()).toMatch(/no outbound/)
  })

  it('renders a friendly empty state', async () => {
    stubFetch([])
    const wrapper = await mountSuspended(FollowupsPanel, { props: { items: [] } })
    expect(wrapper.text()).toContain('No follow-ups due.')
  })

  it('drafts a follow-up and shows the subject and body', async () => {
    stubFetch([{ match: '/draft_followup', method: 'POST', json: DRAFT }])
    const wrapper = await mountSuspended(FollowupsPanel, { props: { items: [ITEMS[0]!] } })

    await buttonNamed(wrapper, /draft/i).trigger('click')
    await flushPromises()

    expect(wrapper.text()).toMatch(/weekend shifts/)
    expect(wrapper.text()).toContain('Follow-up: Analyst application at Initech')
  })

  it('warns when a draft does not clear the mandate gate', async () => {
    stubFetch([{
      match: '/draft_followup',
      method: 'POST',
      json: { ...DRAFT, mandate_ok: false, flags: ['forbidden_client:X'] },
    }])
    const wrapper = await mountSuspended(FollowupsPanel, { props: { items: [ITEMS[0]!] } })

    await buttonNamed(wrapper, /draft/i).trigger('click')
    await flushPromises()

    expect(wrapper.text()).toMatch(/review before sending/i)
    // The flags are shown, not swallowed: the user needs to know why.
    expect(wrapper.text()).toContain('forbidden_client:X')
  })

  it('dismisses a follow-up via the dismiss endpoint', async () => {
    const http = stubFetch([{ match: '/followup/dismiss', method: 'POST', json: { ok: true } }])
    const wrapper = await mountSuspended(FollowupsPanel, { props: { items: [ITEMS[0]!] } })

    await buttonNamed(wrapper, /dismiss/i).trigger('click')
    await flushPromises()

    expect(http.called('/api/jobs/103/followup/dismiss')).toBe(true)
  })

  it('snoozes for seven days, the default the server expects', async () => {
    // Not a V1 case: V1 asserted the dismiss endpoint only, leaving the snooze
    // window (7d, in the button label) unpinned on the request side.
    const http = stubFetch([{ match: '/followup/snooze', method: 'POST', json: { ok: true } }])
    const wrapper = await mountSuspended(FollowupsPanel, { props: { items: [ITEMS[0]!] } })

    await buttonNamed(wrapper, /snooze/i).trigger('click')
    await flushPromises()

    expect(http.called('/api/jobs/103/followup/snooze')).toBe(true)
    expect(http.bodyOf('/followup/snooze')).toEqual({ days: 7 })
  })

  it('emits open with the job id when a row is clicked', async () => {
    // V1 passed `onOpen` and never asserted it fired from this panel; the overview
    // depends on it to navigate, so it is pinned here.
    stubFetch([])
    const wrapper = await mountSuspended(FollowupsPanel, { props: { items: [ITEMS[0]!] } })

    await wrapper.get('.fu-main').trigger('click')
    expect(wrapper.emitted('open')).toEqual([[103]])
  })
})
