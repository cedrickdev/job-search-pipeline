// The evidence page: the write side of the truth guarantee, tested for the two
// things that make it the *evidence* store rather than a profile form.
//
// **A claim cannot be asserted without citing evidence.** The Assert button stays
// disabled until at least one record is checked, because a claim resting on nothing
// is exactly what the guard exists to refuse (`claim_cites_unknown_evidence`). This
// is asserted from the user's side: no evidence checked, no submit.
//
// **A missing profile is its own screen, not an error.** Before onboarding has saved
// a profile, `GET /me/evidence` answers 404 `candidate_profile_not_found`; the page
// says "no profile yet" and points at onboarding, rather than rendering forms that
// would 404 on submit.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import EvidencePage from '~/pages/evidence.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import { claim, evidence, evidenceList, signedIn } from '../support/v2-fixtures'
import type { FetchStub, Route } from '../support/http'
import type { CandidateEvidenceList } from '~/types/v2'
import type { VueWrapper } from '@vue/test-utils'

const EVIDENCE = '/api/v2/me/evidence'
const CLAIMS = '/api/v2/me/claims'

function routes(extra: Route[] = [], list: CandidateEvidenceList = evidenceList()): Route[] {
  return [
    ...extra,
    { match: CLAIMS, method: 'POST', status: 201, json: claim() },
    { match: EVIDENCE, method: 'POST', status: 201, json: evidence() },
    { match: EVIDENCE, method: 'GET', json: list },
    { match: '/api/v2/auth/session', method: 'GET', json: signedIn() },
  ]
}

async function signIn(routeTable: Route[]) {
  const http = stubFetch(routeTable)
  await useSessionStore().ensure()
  return http
}

async function mountPage(...args: Parameters<typeof routes>) {
  const http = await signIn(routes(...args))
  const wrapper = await mountSuspended(EvidencePage, { route: '/evidence' })
  await flushPromises()
  return { http, wrapper }
}

function buttonNamed(wrapper: VueWrapper, label: RegExp) {
  const found = wrapper.findAll('button').find(b => label.test(b.text()))
  if (!found) throw new Error(`no button matching ${label}`)
  return found
}

/**
 * The JSON body of the POST to `fragment`.
 *
 * Not `http.bodyOf`, which returns the first call to a URL: `/me/evidence` is fetched
 * on mount (a GET, no body) before it is written to, so the POST has to be picked out
 * by method.
 */
function postBody(http: FetchStub, fragment: string): Record<string, unknown> {
  const call = http.callsTo(fragment).find(c => c.method === 'POST')
  const body = call?.init?.body
  return (typeof body === 'string' ? JSON.parse(body) : body) as Record<string, unknown>
}

/** The record-a-fact form is first, the assert-a-claim form second. */
async function submitEvidenceForm(wrapper: VueWrapper) {
  await wrapper.findAll('form')[0]!.trigger('submit')
}
async function submitClaimForm(wrapper: VueWrapper) {
  await wrapper.findAll('form')[1]!.trigger('submit')
}

describe('evidence page', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    useSessionStore().$reset()
  })

  it('lists the attested facts and the claims that rest on them', async () => {
    const { wrapper } = await mountPage()

    expect(wrapper.text()).toContain('Backend engineer with 8 years of experience')
    expect(wrapper.text()).toContain('Senior Backend Engineer')
    // A claim shows what it rests on by evidence summary, not a bare id.
    expect(wrapper.text()).toContain('rests on: Backend engineer with 8 years of experience')
  })

  it('records a fact, sending the typed kind and provenance', async () => {
    const { http, wrapper } = await mountPage()

    await wrapper.get('[aria-label="Evidence summary"]').setValue('Shipped the billing rewrite')
    await submitEvidenceForm(wrapper)
    await flushPromises()

    const body = postBody(http, EVIDENCE)
    expect(body.summary).toBe('Shipped the billing rewrite')
    // The form's defaults, sent as the enums the backend takes.
    expect(body.kind).toBe('CV_BULLET')
    expect(body.provenance).toBe('MANUAL_USER_INPUT')
    // An untouched optional field is null, not "": the backend's min_length=1 would 422 on "".
    expect(body.reference_key).toBeNull()
    expect(body.detail).toBeNull()
  })

  it('will not let a claim be asserted until it cites evidence', async () => {
    const { wrapper } = await mountPage()

    await wrapper.get('[aria-label="Claim label"]').setValue('Senior Backend Engineer')
    // Label typed but nothing cited: the guarantee is that a claim rests on evidence,
    // so the button stays disabled.
    expect(buttonNamed(wrapper, /Assert claim/).attributes('disabled')).toBeDefined()

    await wrapper.get(`input[value="66666666-6666-4666-8666-666666666666"]`).setValue(true)
    expect(buttonNamed(wrapper, /Assert claim/).attributes('disabled')).toBeUndefined()
  })

  it('asserts a claim citing the evidence the user checked', async () => {
    const { http, wrapper } = await mountPage()

    await wrapper.get('[aria-label="Claim label"]').setValue('Senior Backend Engineer')
    await wrapper.get(`input[value="66666666-6666-4666-8666-666666666666"]`).setValue(true)
    await submitClaimForm(wrapper)
    await flushPromises()

    const body = postBody(http, CLAIMS)
    expect(body.label).toBe('Senior Backend Engineer')
    expect(body.evidence_ids).toEqual(['66666666-6666-4666-8666-666666666666'])
  })

  it('surfaces a rejected claim in its own words rather than throwing', async () => {
    const { wrapper } = await mountPage([{
      match: CLAIMS,
      method: 'POST',
      status: 422,
      json: { error: 'claim_cites_unknown_evidence', detail: 'no such evidence' },
    }])

    await wrapper.get('[aria-label="Claim label"]').setValue('Senior Backend Engineer')
    await wrapper.get(`input[value="66666666-6666-4666-8666-666666666666"]`).setValue(true)
    await submitClaimForm(wrapper)
    await flushPromises()

    expect(wrapper.get('.auth-error').text())
      .toContain('That claim refers to evidence that is no longer on file')
  })

  it('says finish onboarding first when there is no profile yet', async () => {
    const { wrapper } = await mountPage([{
      match: EVIDENCE,
      method: 'GET',
      status: 404,
      json: { error: 'candidate_profile_not_found', detail: 'no profile' },
    }])

    expect(wrapper.text()).toContain('No profile yet')
    // Not an error screen, and no forms that would 404 on submit.
    expect(wrapper.find('.auth-error').exists()).toBe(false)
    expect(wrapper.find('[aria-label="Evidence summary"]').exists()).toBe(false)
  })
})
