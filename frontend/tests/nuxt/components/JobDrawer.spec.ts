// Ported from webapp/src/components/JobDrawer.test.tsx — the largest V1 test file,
// because the drawer is where every state change happens.
//
// All twenty V1 cases are here. Five are new: the Fit tab (FitPanel shipped in V1
// with no test), the cancel path of the confirmation gate, Apply-now's hidden
// branch when no CV has been rendered, the status select reverting while the
// confirmation is open, and `close`. The first four are behaviours the port's own
// header comment claims, and a claim with no test is a claim that decays.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import JobDrawer from '~/components/JobDrawer.vue'
import type { JobDetail } from '~/types/domain'
import { stubFetch } from '../support/http'

function detail(overrides: Partial<JobDetail> = {}): JobDetail {
  return {
    job: {
      id: 10,
      company: 'Alpha',
      title: 'Shift Lead',
      url: 'https://example.invalid/10',
      location: 'Lausanne',
      language: 'en',
      description: 'Retail and service.',
    },
    application: { id: 1, status: 'Ready to apply', submitted_at: null, recruiter_email: null },
    score: { score: 91, reasoning: 'Strong match' },
    fit: {
      available: true,
      coverage_score: 0.82,
      risk_tier: 'GREEN',
      matched_keywords: ['python'],
      missing_keywords: ['spark'],
    },
    cv_versions: {
      en: { id: 5, language: 'en', phone_screen_pct: 88, pdf_url: '/api/files/cv/5', created_at: '2026-06-11T07:00:00' },
      fr: null,
    },
    cover_letter: { en: null, fr: null },
    events: [
      { id: 2, event_type: 'status_change', detail: 'Ready to apply', source: 'manual', created_at: '2026-06-12T10:00:00' },
      { id: 1, event_type: 'discovered', detail: 'found on wtj', source: 'pipeline', created_at: '2026-06-10T09:00:00' },
    ],
    pending_regen: null,
    last_regen: null,
    last_apply: null,
    ...overrides,
  }
}

const IDLE = { state: 'idle', kind: null, started_at: null, last_run: null }
const FULL_RUNNING = {
  state: 'running',
  kind: 'full',
  started_at: '2026-06-16T08:30:00',
  last_run: null,
}

const QUEUED = {
  request_id: 42,
  notes: 'Lead on NLP and LLMs',
  creativity: 'bold',
  created_at: '2026-06-16T08:00:00',
}

// A CV regeneration queued for the next agentic run: while queued, last_regen
// mirrors the pending request.
const REGEN_QUEUED = detail({
  pending_regen: QUEUED,
  last_regen: { ...QUEUED, status: 'pending', resolved_at: null, detail: null },
})
// Failed after retries — no longer pending, reason recorded.
const REGEN_FAILED = detail({
  last_regen: {
    ...QUEUED,
    status: 'failed',
    resolved_at: '2026-06-16T09:00:00',
    detail: 'tailoring failed after 3 attempts',
  },
})
// Completed and applied by a background run.
const REGEN_DONE = detail({
  last_regen: {
    request_id: 42,
    status: 'done',
    notes: 'More econometrics',
    creativity: 'balanced',
    created_at: '2026-06-16T08:00:00',
    resolved_at: '2026-06-16T09:00:00',
    detail: null,
  },
})

const APPLY = {
  request_id: 7,
  channel: 'linkedin',
  detail: null,
  screenshot_path: null,
  created_at: '2026-06-18T08:00:00',
  resolved_at: null,
} as const

const APPLYING = detail({ last_apply: { ...APPLY, status: 'in_progress' } })
const APPLIED = detail({
  application: { id: 1, status: 'Applied', submitted_at: null, recruiter_email: null },
  last_apply: {
    ...APPLY,
    status: 'applied',
    detail: 'Submitted via Easy Apply',
    screenshot_path: 'data/screenshots/10_done.png',
    resolved_at: '2026-06-18T08:03:00',
  },
})
const NEEDS_YOU = detail({
  last_apply: {
    ...APPLY,
    status: 'needs_you',
    detail: 'Browser in use. Close the live session and retry.',
    resolved_at: '2026-06-18T08:00:05',
  },
})
const APPLY_FAILED = detail({
  last_apply: {
    ...APPLY,
    status: 'failed',
    channel: 'wtj',
    detail: 'Unhandled exception: boom',
    resolved_at: '2026-06-18T08:00:09',
  },
})

/**
 * Job detail + run status (which drives the queued/regenerating chip) + a 202 for
 * every POST, like V1's URL-aware stub. The POST route comes first so an action
 * never falls through to a GET route.
 */
function api(data: JobDetail, runStatus: unknown = IDLE) {
  return stubFetch([
    { match: '/api/', method: 'POST', status: 202, json: { ok: true } },
    { match: '/api/runs/status', method: 'GET', json: runStatus },
    { match: '/api/chat/history', method: 'GET', json: { messages: [] } },
    { match: '/api/jobs/10', method: 'GET', json: data },
  ])
}

/** Mount the drawer over that API state, with the fetch handle for assertions. */
async function open(data: JobDetail, runStatus: unknown = IDLE) {
  const http = api(data, runStatus)
  const wrapper = await mountSuspended(JobDrawer, { props: { jobId: 10 } })
  await flushPromises()
  return { wrapper, http }
}

// `VueWrapper`, not `Awaited<ReturnType<typeof mountSuspended>>`: that resolves
// to `any` through the generic, which unties every helper below from the DOM API.
type Wrapper = VueWrapper

function buttonNamed(wrapper: Wrapper, label: string) {
  const found = wrapper.findAll('button').find(b => b.text() === label)
  if (!found) throw new Error(`no button labelled ${label}`)
  return found
}

function hasButton(wrapper: Wrapper, label: string) {
  return wrapper.findAll('button').some(b => b.text() === label)
}

/** Click a tab by its rendered label ('CV', 'Activity', …). */
async function openTab(wrapper: Wrapper, label: string) {
  await buttonNamed(wrapper, label).trigger('click')
  await flushPromises()
}

/** Accept the pending confirmation. */
async function confirm(wrapper: Wrapper) {
  await buttonNamed(wrapper, 'Confirm').trigger('click')
  await flushPromises()
}

function posts(http: ReturnType<typeof stubFetch>) {
  return http.calls.filter(c => c.method === 'POST')
}

describe('JobDrawer', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
  })

  it('renders job detail', async () => {
    const { wrapper } = await open(detail())
    const text = wrapper.text()

    expect(text).toContain('Alpha')
    expect(text).toContain('Shift Lead')
    expect(text).toContain('91')
    expect(text).toContain('Strong match')
  })

  it('shows the activity timeline on the Activity tab', async () => {
    const { wrapper } = await open(detail())
    await openTab(wrapper, 'Activity')

    const rows = wrapper.findAll('.drawer-activity li').map(li => li.text())
    // Newest first, as the backend returns them.
    expect(rows[0]).toContain('status_change')
    expect(rows[1]).toContain('discovered')
    expect(rows[1]).toContain('found on wtj')
    expect(rows[1]).toContain('(pipeline)')
  })

  it('shows the keyword-coverage summary on the Fit tab', async () => {
    // FitPanel shipped in V1 with no test of its own.
    const { wrapper } = await open(detail())
    await openTab(wrapper, 'Fit')

    expect(wrapper.text()).toContain('82%')
    expect(wrapper.text()).toContain('GREEN')
    expect(wrapper.text()).toContain('Missing: spark')
  })

  it('shows a job-scoped copilot on the Copilot tab', async () => {
    const { wrapper } = await open(detail())
    await openTab(wrapper, 'Copilot')

    expect(wrapper.find('section[aria-label="Copilot"]').exists()).toBe(true)
    expect(wrapper.find('[aria-label="Message copilot"]').exists()).toBe(true)
  })

  it('requires confirm before posting an action', async () => {
    const { wrapper, http } = await open(detail())

    await buttonNamed(wrapper, 'Approve').trigger('click')
    await flushPromises()
    // Nothing has been sent yet — the confirmation bar is waiting.
    expect(posts(http)).toHaveLength(0)
    expect(wrapper.get('[role="alertdialog"]').text()).toContain('Approve Alpha')

    await confirm(wrapper)
    expect(http.callsTo('/api/jobs/10/go').map(c => c.method)).toEqual(['POST'])
  })

  it('posts nothing when the confirmation is cancelled', async () => {
    // Not a V1 case. The gate is only a gate if declining it is inert.
    const { wrapper, http } = await open(detail())

    await buttonNamed(wrapper, 'Skip').trigger('click')
    await buttonNamed(wrapper, 'Cancel').trigger('click')
    await flushPromises()

    expect(wrapper.find('[role="alertdialog"]').exists()).toBe(false)
    expect(posts(http)).toHaveLength(0)
  })

  it('shows a queued CV-regeneration chip with its creativity when no run is active', async () => {
    const { wrapper } = await open(REGEN_QUEUED, IDLE)

    // Queued, not running: honest wording plus the inferred boldness level.
    expect(wrapper.get('.regen-chip').text()).toContain('CV regen queued (bold)')
    expect(wrapper.text()).not.toMatch(/regenerating now/i)
    // The notes ride along as a tooltip rather than crowding the header.
    expect(wrapper.get('.regen-chip').attributes('title')).toBe('Lead on NLP and LLMs')
  })

  it('shows a \'regenerating now\' chip while a full run is active', async () => {
    // A live full run is the only thing that consumes the regen queue, so this is
    // the only state in which the chip may claim real progress.
    const { wrapper } = await open(REGEN_QUEUED, FULL_RUNNING)

    expect(wrapper.get('.regen-chip').text()).toMatch(/regenerating now/i)
    expect(wrapper.text()).not.toMatch(/regen queued/i)
  })

  it('shows a failed chip when the last regeneration failed', async () => {
    const { wrapper } = await open(REGEN_FAILED, IDLE)
    expect(wrapper.get('.regen-chip-failed').text()).toMatch(/CV regen failed/i)
  })

  it('shows no regen indicator when nothing is pending or recorded', async () => {
    const { wrapper } = await open(detail())
    expect(wrapper.find('.regen-chip').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/CV regen/i)
  })

  it('explains the queued CV regeneration with its notes and creativity on the CV tab', async () => {
    const { wrapper } = await open(REGEN_QUEUED, IDLE)
    await openTab(wrapper, 'CV')

    const banner = wrapper.get('.regen-banner')
    expect(banner.attributes('role')).toBe('status')
    expect(banner.text()).toMatch(/regeneration queued/i)
    expect(banner.text()).toContain('bold')
    expect(banner.text()).toContain('Lead on NLP and LLMs')
  })

  it('processes a queued regen via the Run-now button (POSTs /api/runs/full)', async () => {
    const { wrapper, http } = await open(REGEN_QUEUED, IDLE)
    await openTab(wrapper, 'CV')

    await buttonNamed(wrapper, 'Run pipeline now').trigger('click')
    await flushPromises()

    expect(http.callsTo('/api/runs/full').map(c => c.method)).toEqual(['POST'])
  })

  it('disables the Run-now button while a full run is active', async () => {
    const { wrapper } = await open(REGEN_QUEUED, FULL_RUNNING)
    await openTab(wrapper, 'CV')

    expect(buttonNamed(wrapper, 'Run pipeline now').attributes('disabled')).toBeDefined()
  })

  it('shows the failure reason and a retry on the CV tab when a regen failed', async () => {
    const { wrapper } = await open(REGEN_FAILED, IDLE)
    await openTab(wrapper, 'CV')

    const banner = wrapper.get('.regen-banner')
    expect(banner.text()).toMatch(/regeneration failed/i)
    expect(banner.text()).toContain('tailoring failed after 3 attempts')
    // A failure offers a retry through the same full-run trigger.
    expect(hasButton(wrapper, 'Run pipeline now')).toBe(true)
  })

  it('confirms a completed regeneration on the CV tab', async () => {
    const { wrapper } = await open(REGEN_DONE, IDLE)
    await openTab(wrapper, 'CV')

    expect(wrapper.get('.regen-banner').text()).toMatch(/regeneration applied/i)
    // Nothing left to trigger once it landed.
    expect(hasButton(wrapper, 'Run pipeline now')).toBe(false)
  })

  it('shows an Apply-now button for a ready job with a CV', async () => {
    const { wrapper } = await open(detail(), IDLE)
    expect(hasButton(wrapper, 'Apply now')).toBe(true)
  })

  it('hides Apply-now for a ready job with no rendered CV', async () => {
    // Not a V1 case. Applying without a CV is the one combination the backend
    // cannot make good, so the button must not be reachable.
    const { wrapper } = await open(detail({ cv_versions: { en: null, fr: null } }), IDLE)
    expect(hasButton(wrapper, 'Apply now')).toBe(false)
  })

  it('fires Apply-now for a ready job with a CV', async () => {
    const { wrapper, http } = await open(detail(), IDLE)

    await buttonNamed(wrapper, 'Apply now').trigger('click')
    await confirm(wrapper)

    expect(http.callsTo('/api/jobs/10/apply-now').map(c => c.method)).toEqual(['POST'])
  })

  it('shows Applying-now and hides the Apply-now button while mid-apply', async () => {
    const { wrapper } = await open(APPLYING, IDLE)

    const banner = wrapper.get('.apply-banner')
    expect(banner.text()).toMatch(/applying now/i)
    expect(banner.text()).toContain('(linkedin)')
    // A second submission while a worker is mid-apply would be a duplicate.
    expect(hasButton(wrapper, 'Apply now')).toBe(false)
  })

  it('shows an Applied banner', async () => {
    const { wrapper } = await open(APPLIED, IDLE)

    const banner = wrapper.get('.apply-banner-done')
    expect(banner.text()).toContain('✓ Applied')
    expect(banner.text()).toContain('Submitted via Easy Apply')
    expect(banner.text()).toContain('data/screenshots/10_done.png')
  })

  it('shows a Needs-you banner and retries via Apply-now', async () => {
    const { wrapper, http } = await open(NEEDS_YOU, IDLE)

    const banner = wrapper.get('.apply-banner-warn')
    expect(banner.text()).toContain('⚠ Needs you')
    expect(banner.text()).toContain('Browser in use')

    await buttonNamed(wrapper, 'Retry').trigger('click')
    await confirm(wrapper)
    expect(http.callsTo('/api/jobs/10/apply-now').map(c => c.method)).toEqual(['POST'])
  })

  it('shows a Failed banner with the reason', async () => {
    const { wrapper } = await open(APPLY_FAILED, IDLE)

    const banner = wrapper.get('.apply-banner-failed')
    expect(banner.text()).toContain('✕ Failed')
    expect(banner.text()).toContain('Unhandled exception: boom')
    expect(hasButton(wrapper, 'Retry')).toBe(true)
  })

  it('changes the status to any value through the confirm-gated selector', async () => {
    const { wrapper, http } = await open(detail())

    // An arbitrary lifecycle status the fixed Approve/Mark applied/Skip buttons
    // cannot reach.
    await wrapper.get('[aria-label="Change status"]').setValue('Interview scheduled')
    expect(posts(http)).toHaveLength(0)
    expect(wrapper.get('[role="alertdialog"]').text())
      .toContain('Change status to Interview scheduled')

    await confirm(wrapper)
    const call = http.callsTo('/api/jobs/10/status')[0]!
    expect(call.method).toBe('POST')
    expect(JSON.parse(String(call.init?.body))).toEqual({ status: 'Interview scheduled' })
  })

  it('leaves the status select on the current value while the change is unconfirmed', async () => {
    // Not a V1 case. The select is reverted the moment it changes, so cancelling
    // cannot leave the header claiming a status the backend never received.
    const { wrapper, http } = await open(detail())
    const select = wrapper.get('[aria-label="Change status"]')

    await select.setValue('Offer')
    expect((select.element as HTMLSelectElement).value).toBe('Ready to apply')

    await buttonNamed(wrapper, 'Cancel').trigger('click')
    await flushPromises()
    expect((select.element as HTMLSelectElement).value).toBe('Ready to apply')
    expect(posts(http)).toHaveLength(0)
  })

  it('emits close from the Close button and from the backdrop', async () => {
    // Not a V1 case: V1 passed an `onClose` prop and never asserted it fired.
    const { wrapper } = await open(detail())

    await buttonNamed(wrapper, 'Close').trigger('click')
    await wrapper.get('.drawer-backdrop').trigger('click')

    expect(wrapper.emitted('close')).toHaveLength(2)
  })
})
