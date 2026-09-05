// The saved-search form. Same shape of test as ProfileForm's, one level harder,
// because a saved search is what a discovery run actually reads: an area silently
// dropped here is a region of the map nobody searches, and an emptied keyword list
// is a filter that stops filtering.
//
// Three groups: what it puts on screen, what a save must not erase (the wholesale
// `PUT` again, plus the RADIUS areas this build cannot render), and the small
// conversions — one keyword per line, `CH` upper-cased, an empty list meaning "no
// restriction".
import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import SearchProfileForm from '~/components/SearchProfileForm.vue'
import { search, searchDraft } from '../support/v2-fixtures'
import { ApiError } from '~/utils/api-client'
import type { SearchArea, SearchProfile, SearchProfileDraft } from '~/types/v2'
import type { VueWrapper } from '@vue/test-utils'

/** A radius area: nothing in this build can create one, only preserve one. */
const RADIUS: SearchArea = {
  kind: 'RADIUS',
  center: { latitude: 46.52, longitude: 6.63 },
  radius_km: 30,
  label: 'Around Lausanne',
}

function mountForm(props: {
  search?: SearchProfile | null
  pending?: boolean
  error?: unknown
  submitLabel?: string
} = {}) {
  return mountSuspended(SearchProfileForm, { props: { search: null, ...props } })
}

function submitted(wrapper: VueWrapper): SearchProfileDraft {
  const events = wrapper.emitted('submit')
  expect(events, 'the form emitted no submit').toBeTruthy()
  return events!.at(-1)![0] as SearchProfileDraft
}

const SUBMIT = 'button[type="submit"]'
/** The "Run this search" box, told apart from the type and mode checkboxes. */
const RUNS = '.acct-form > .acct-check input'

describe('SearchProfileForm', () => {
  // `areas` is required and an empty list is a guaranteed 422, so a new search is
  // given the row it cannot do without rather than an empty fieldset.
  it('opens a new search with one empty country row, set to run', async () => {
    const wrapper = await mountForm()

    expect(wrapper.findAll('[aria-label$="kind"]')).toHaveLength(1)
    expect(wrapper.get<HTMLSelectElement>('[aria-label="Area 1 kind"]').element.value)
      .toBe('COUNTRY')
    expect(wrapper.get<HTMLInputElement>('[aria-label="Area 1 country"]').element.value).toBe('')
    expect(wrapper.get<HTMLInputElement>(RUNS).element.checked).toBe(true)
    // Only the name is missing.
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined()
  })

  it('fills itself from a saved search', async () => {
    const wrapper = await mountForm({
      search: search({
        search: searchDraft({
          name: 'Backend roles',
          is_active: false,
          areas: [
            { kind: 'COUNTRY', country: 'CH', label: null },
            { kind: 'REMOTE_ONLY', country: null, label: null },
          ],
          queries: ['backend engineer', 'platform engineer'],
          title_keywords: ['python'],
          excluded_keywords: ['sales'],
          opportunity_types: ['FULL_TIME'],
          workplace_modes: ['HYBRID', 'REMOTE'],
        }),
      }),
    })

    expect(wrapper.get<HTMLInputElement>('#search-name').element.value).toBe('Backend roles')
    expect(wrapper.get<HTMLInputElement>(RUNS).element.checked).toBe(false)
    expect(wrapper.findAll('[aria-label$="kind"]')).toHaveLength(2)
    expect(wrapper.get<HTMLSelectElement>('[aria-label="Area 2 kind"]').element.value)
      .toBe('REMOTE_ONLY')
    // One entry per line — a comma-separated box would break on "Legal, Compliance".
    expect(wrapper.get<HTMLTextAreaElement>('#search-queries').element.value)
      .toBe('backend engineer\nplatform engineer')
    expect(wrapper.get<HTMLTextAreaElement>('#search-excluded').element.value).toBe('sales')

    const ticked = wrapper.findAll<HTMLInputElement>('.acct-checks input')
      .filter(box => box.element.checked)
      .map(box => box.element.value)
    expect(ticked).toEqual(['FULL_TIME', 'HYBRID', 'REMOTE'])
  })

  it('lists a radius area read-only, saying who will edit it', async () => {
    const wrapper = await mountForm({
      search: search({ search: searchDraft({ areas: [{ kind: 'COUNTRY', country: 'CH', label: null }, RADIUS] }) }),
    })

    // One editable row for the country; the radius is a line of text, not a control.
    expect(wrapper.findAll('[aria-label$="kind"]')).toHaveLength(1)
    const kept = wrapper.get('.acct-kept')
    expect(kept.text()).toContain('Around Lausanne')
    expect(kept.text()).toContain('the map view will edit these')
  })

  it('describes an unlabelled radius by its own numbers', async () => {
    const wrapper = await mountForm({
      search: search({ search: searchDraft({ areas: [{ ...RADIUS, label: null }] }) }),
    })
    expect(wrapper.get('.acct-kept').text()).toContain('30 km around 46.52, 6.63')
  })

  it('will not submit a search with no name', async () => {
    const wrapper = await mountForm()
    await wrapper.get('#search-name').setValue('  ')
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined()
  })

  // A search with no area is a search of the planet, and the API refuses it.
  it('will not submit a search with no area left', async () => {
    const wrapper = await mountForm()
    await wrapper.get('#search-name').setValue('Anywhere')
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeUndefined()

    await wrapper.get('[aria-label="Remove area 1"]').trigger('click')
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined()
  })

  // A radius counts: it is an area, even though this form cannot render it as a row.
  it('accepts a search whose only area is a preserved one', async () => {
    const wrapper = await mountForm({
      search: search({ search: searchDraft({ areas: [RADIUS] }) }),
    })

    expect(wrapper.findAll('[aria-label$="kind"]')).toHaveLength(1)
    await wrapper.get('[aria-label="Remove area 1"]').trigger('click')

    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeUndefined()
  })
})

/**
 * The wholesale-`PUT` cases, and the ones that would quietly change where a
 * discovery run looks.
 */
describe('SearchProfileForm · what a save must not erase', () => {
  it('keeps the filters it has no editor for', async () => {
    const saved = search({
      search: searchDraft({
        contract_types: ['PERMANENT'],
        posting_languages: ['fr', 'de'],
        source_keys: ['jobup'],
        workload: { min_percent: 60, max_percent: 100 },
      }),
    })
    const wrapper = await mountForm({ search: saved })

    await wrapper.get('#search-name').setValue('Renamed')
    await wrapper.get('form').trigger('submit')

    const draft = submitted(wrapper)
    expect(draft.name).toBe('Renamed')
    expect(draft.contract_types).toEqual(['PERMANENT'])
    expect(draft.posting_languages).toEqual(['fr', 'de'])
    expect(draft.source_keys).toEqual(['jobup'])
    expect(draft.workload).toEqual({ min_percent: 60, max_percent: 100 })
  })

  // Dropping the radius would narrow the search without saying so — and this form is
  // where an edit to any other field passes through.
  it('sends a radius area back unchanged', async () => {
    const wrapper = await mountForm({
      search: search({
        search: searchDraft({ areas: [{ kind: 'COUNTRY', country: 'CH', label: null }, RADIUS] }),
      }),
    })

    await wrapper.get('#search-name').setValue('Still both areas')
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).areas).toEqual([
      { kind: 'COUNTRY', country: 'CH', label: null },
      RADIUS,
    ])
  })

  it('keeps a radius when the editable row beside it is removed', async () => {
    const wrapper = await mountForm({
      search: search({
        search: searchDraft({ areas: [{ kind: 'COUNTRY', country: 'CH', label: null }, RADIUS] }),
      }),
    })

    await wrapper.get('[aria-label="Remove area 1"]').trigger('click')
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).areas).toEqual([RADIUS])
  })

  it('keeps a label the map gave a country area', async () => {
    const wrapper = await mountForm({
      search: search({
        search: searchDraft({ areas: [{ kind: 'COUNTRY', country: 'CH', label: 'Switzerland' }] }),
      }),
    })

    await wrapper.get('form').trigger('submit')
    expect(submitted(wrapper).areas).toEqual([
      { kind: 'COUNTRY', country: 'CH', label: 'Switzerland' },
    ])
  })
})

describe('SearchProfileForm · what it sends', () => {
  /** A new search with just enough filled in to be submittable. */
  async function drafted(name = 'Backend roles') {
    const wrapper = await mountForm()
    await wrapper.get('#search-name').setValue(name)
    await wrapper.get('[aria-label="Area 1 country"]').setValue('ch')
    return wrapper
  }

  function addArea(wrapper: VueWrapper) {
    return wrapper.findAll('button').find(b => b.text().includes('Add an area'))!
  }

  it('upper-cases a country code and trims the name', async () => {
    const wrapper = await drafted('  Backend roles  ')
    await wrapper.get('form').trigger('submit')

    const draft = submitted(wrapper)
    expect(draft.name).toBe('Backend roles')
    expect(draft.areas).toEqual([{ kind: 'COUNTRY', country: 'CH', label: null }])
  })

  // A remote-only area may name a country or not, and "" is not a country: the
  // schema's `country` is nullable and `extra="forbid"` would refuse a stray field.
  it('sends a remote-only area with no country as null', async () => {
    const wrapper = await mountForm()
    await wrapper.get('#search-name').setValue('Anywhere remote')
    await wrapper.get('[aria-label="Area 1 kind"]').setValue('REMOTE_ONLY')
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).areas).toEqual([
      { kind: 'REMOTE_ONLY', country: null, label: null },
    ])
  })

  it('narrows a remote-only area to a country when one is given', async () => {
    const wrapper = await drafted('Remote in Switzerland')
    await wrapper.get('[aria-label="Area 1 kind"]').setValue('REMOTE_ONLY')
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).areas).toEqual([
      { kind: 'REMOTE_ONLY', country: 'CH', label: null },
    ])
  })

  // Switching a row's kind must not carry the previous kind's keys along: every V2
  // schema is `extra="forbid"`, so a `center` left on a country area is a 422 about a
  // field the user cannot see.
  it('adds a second area without disturbing the first', async () => {
    const wrapper = await drafted()
    await addArea(wrapper).trigger('click')
    await wrapper.get('[aria-label="Area 2 kind"]').setValue('REMOTE_ONLY')
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).areas).toEqual([
      { kind: 'COUNTRY', country: 'CH', label: null },
      { kind: 'REMOTE_ONLY', country: null, label: null },
    ])
  })

  it('reads one keyword per line and ignores blank ones', async () => {
    const wrapper = await drafted()
    await wrapper.get('#search-queries').setValue('backend engineer\n\n  platform engineer  \n')
    await wrapper.get('#search-titles').setValue('python')
    await wrapper.get('#search-excluded').setValue('sales\nrecruitment agency')
    await wrapper.get('form').trigger('submit')

    const draft = submitted(wrapper)
    expect(draft.queries).toEqual(['backend engineer', 'platform engineer'])
    expect(draft.title_keywords).toEqual(['python'])
    // Kept whole: a comma-separated box would have split this one in two.
    expect(draft.excluded_keywords).toEqual(['sales', 'recruitment agency'])
  })

  // An empty list is the domain's "no restriction". Ticking nothing must send nothing,
  // not every box.
  it('sends empty filter lists when nothing is ticked', async () => {
    const wrapper = await drafted()
    await wrapper.get('form').trigger('submit')

    const draft = submitted(wrapper)
    expect(draft.opportunity_types).toEqual([])
    expect(draft.workplace_modes).toEqual([])
    expect(draft.queries).toEqual([])
  })

  it('sends the types and modes that were ticked', async () => {
    const wrapper = await drafted()
    await wrapper.get('.acct-checks input[value="INTERNSHIP"]').setValue(true)
    await wrapper.get('.acct-checks input[value="REMOTE"]').setValue(true)
    await wrapper.get('form').trigger('submit')

    const draft = submitted(wrapper)
    expect(draft.opportunity_types).toEqual(['INTERNSHIP'])
    expect(draft.workplace_modes).toEqual(['REMOTE'])
  })

  it('sends the paused flag as the box was left', async () => {
    const wrapper = await drafted()
    await wrapper.get(RUNS).setValue(false)
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).is_active).toBe(false)
  })
})

describe('SearchProfileForm · refusals and pending state', () => {
  it('shows a refusal as one sentence, in an alert', async () => {
    const wrapper = await mountForm({
      search: search(),
      error: new ApiError(404, 'gone', {
        error: 'search_profile_not_found',
        detail: 'gone',
      }),
    })

    expect(wrapper.get('.auth-error').text()).toBe('That saved search no longer exists.')
  })

  it('puts a rejected area message in the area fieldset', async () => {
    const wrapper = await mountForm({
      search: search(),
      error: new ApiError(422, 'invalid', {
        error: 'validation_failed',
        detail: 'invalid',
        errors: [{ loc: ['body', 'areas', 0, 'country'], msg: 'not a country code' }],
      }),
    })

    expect(wrapper.get('.acct-fieldset .auth-field-error').text()).toBe('not a country code')
  })

  it('locks every control while a save is in flight', async () => {
    const wrapper = await mountForm({ search: search(), pending: true })

    expect(wrapper.get(SUBMIT).text()).toBe('Saving…')
    for (const control of wrapper.findAll('input, select, textarea')) {
      expect(control.attributes('disabled')).toBeDefined()
    }
  })

  it('uses the button label its page gives it', async () => {
    const wrapper = await mountForm({ search: null, submitLabel: 'Save and continue' })
    expect(wrapper.get(SUBMIT).text()).toBe('Save and continue')
  })
})
