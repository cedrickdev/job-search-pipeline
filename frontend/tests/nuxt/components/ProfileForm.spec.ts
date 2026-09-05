// The candidate profile form. Mostly one thing: what the emitted draft contains.
//
// `PUT /api/v2/me/profile` replaces the whole profile, so the interesting failure
// this file guards against is not a wrong-looking input — it is a *correct-looking*
// save that silently drops a field the form does not render. Half the cases below
// are about fields nobody typed into.
//
// The form is tested through its emit rather than through a stubbed `fetch`: it does
// not know how to save anything, and the pages that do own that half
// (pages/profile.vue, pages/onboarding.vue).
import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import ProfileForm from '~/components/ProfileForm.vue'
import { profile, profileDraft } from '../support/v2-fixtures'
import { ApiError } from '~/utils/api-client'
import type { CandidateProfile, CandidateProfileDraft } from '~/types/v2'
import type { VueWrapper } from '@vue/test-utils'

function mountForm(props: {
  profile?: CandidateProfile | null
  pending?: boolean
  error?: unknown
  submitLabel?: string
} = {}) {
  return mountSuspended(ProfileForm, { props: { profile: null, ...props } })
}

/** The draft the form last asked its page to save. */
function submitted(wrapper: VueWrapper): CandidateProfileDraft {
  const events = wrapper.emitted('submit')
  expect(events, 'the form emitted no submit').toBeTruthy()
  return events!.at(-1)![0] as CandidateProfileDraft
}

const SUBMIT = 'button[type="submit"]'

describe('ProfileForm', () => {
  it('starts empty for an account with no profile yet', async () => {
    const wrapper = await mountForm()

    expect(wrapper.get<HTMLInputElement>('#profile-display-name').element.value).toBe('')
    expect(wrapper.get<HTMLInputElement>('#profile-city').element.value).toBe('')
    expect(wrapper.findAll('[aria-label$="code"]')).toHaveLength(0)
    // Nothing to save yet: the profile's one required field is empty.
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined()
  })

  it('will not submit a name that is only whitespace', async () => {
    const wrapper = await mountForm()
    await wrapper.get('#profile-display-name').setValue('   ')
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined()
  })

  it('fills every box it renders from the saved profile', async () => {
    const wrapper = await mountForm({
      profile: profile({
        profile: profileDraft({
          display_name: 'Test Candidate',
          headline: 'Backend engineer',
          base_location: { city: 'Lausanne', country: 'CH' },
          languages: [{ language: 'fr', level: 'NATIVE' }, { language: 'en', level: 'C1' }],
        }),
      }),
    })

    expect(wrapper.get<HTMLInputElement>('#profile-display-name').element.value)
      .toBe('Test Candidate')
    expect(wrapper.get<HTMLInputElement>('#profile-headline').element.value)
      .toBe('Backend engineer')
    expect(wrapper.get<HTMLInputElement>('#profile-city').element.value).toBe('Lausanne')
    expect(wrapper.get<HTMLInputElement>('#profile-country').element.value).toBe('CH')
    expect(wrapper.get<HTMLSelectElement>('[aria-label="Language 2 level"]').element.value)
      .toBe('C1')
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeUndefined()
  })

  // The page renders the form before its query has answered, so the profile arrives
  // as a prop change rather than an initial value.
  it('seeds the boxes when the profile arrives after mount', async () => {
    const wrapper = await mountForm()
    await wrapper.setProps({
      profile: profile({ profile: profileDraft({ display_name: 'Arrived Late' }) }),
    })

    expect(wrapper.get<HTMLInputElement>('#profile-display-name').element.value)
      .toBe('Arrived Late')
  })
})

/**
 * The wholesale-`PUT` cases.
 *
 * Each of these would pass a review of the form's own markup — the visible fields
 * are all correct — and each would lose data the account had already given.
 */
describe('ProfileForm · what a save must not erase', () => {
  it('keeps the fields it has no editor for', async () => {
    const saved = profile({
      profile: profileDraft({
        display_name: 'Test Candidate',
        work_authorizations: [{ country: 'CH', status: 'CITIZEN', evidence_ids: [] }],
        availability: { earliest_start: '2026-03-01', notice_period_days: 30, weekly_slots: [] },
      }),
    })
    const wrapper = await mountForm({ profile: saved })

    await wrapper.get('#profile-headline').setValue('Platform engineer')
    await wrapper.get('form').trigger('submit')

    const draft = submitted(wrapper)
    expect(draft.headline).toBe('Platform engineer')
    expect(draft.work_authorizations).toEqual(saved.profile.work_authorizations)
    expect(draft.availability).toEqual(saved.profile.availability)
  })

  // `base_location` carries a geocode the form never shows. Rebuilding the location
  // from the two boxes would drop the point, and with it every radius search that
  // resolves against it.
  it('keeps the geocode of a location whose city it edits', async () => {
    const saved = profile({
      profile: profileDraft({
        display_name: 'Test Candidate',
        base_location: {
          city: 'Lausanne',
          country: 'CH',
          region: 'VD',
          postal_code: '1000',
          raw: 'Lausanne, VD, Switzerland',
          point: { latitude: 46.52, longitude: 6.63 },
        },
      }),
    })
    const wrapper = await mountForm({ profile: saved })

    await wrapper.get('#profile-city').setValue('Genève')
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).base_location).toEqual({
      ...saved.profile.base_location,
      city: 'Genève',
    })
  })
})

describe('ProfileForm · what it sends', () => {
  /** A form filled in by hand, as a first-time user would. */
  async function filled(name = 'New Person') {
    const wrapper = await mountForm()
    await wrapper.get('#profile-display-name').setValue(name)
    return wrapper
  }

  /** By its words: the fieldset also contains one remove button per row. */
  function addLanguage(wrapper: VueWrapper) {
    return wrapper.findAll('button').find(b => b.text().includes('Add a language'))!
  }

  // A `Location` has to locate something — the domain rejects one with every field
  // unset — so two empty boxes mean "not given", not "a location that says nothing".
  it('sends no location at all when both boxes are empty', async () => {
    const wrapper = await filled()
    await wrapper.get('form').trigger('submit')
    expect(submitted(wrapper).base_location).toBeNull()
  })

  it('sends a location as soon as either box is filled', async () => {
    const wrapper = await filled()
    await wrapper.get('#profile-country').setValue('ch')
    await wrapper.get('form').trigger('submit')

    // Upper-cased for the server, which compares ISO codes; the city is left as typed.
    expect(submitted(wrapper).base_location).toEqual({ city: null, country: 'CH' })
  })

  it('trims the name and drops an empty headline', async () => {
    const wrapper = await filled('  Test Candidate  ')
    await wrapper.get('form').trigger('submit')

    const draft = submitted(wrapper)
    expect(draft.display_name).toBe('Test Candidate')
    expect(draft.headline).toBeNull()
  })

  it('lower-cases language codes and keeps their levels', async () => {
    const wrapper = await filled()
    await addLanguage(wrapper).trigger('click')
    await wrapper.get('[aria-label="Language 1 code"]').setValue('FR')
    await wrapper.get('[aria-label="Language 1 level"]').setValue('NATIVE')
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).languages).toEqual([{ language: 'fr', level: 'NATIVE' }])
  })

  // An added row starts empty, and a user who adds one by accident should not be
  // told off by a 422 for a language they never named.
  it('drops a language row left blank', async () => {
    const wrapper = await filled()
    await addLanguage(wrapper).trigger('click')
    await addLanguage(wrapper).trigger('click')
    await wrapper.get('[aria-label="Language 1 code"]').setValue('de')
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).languages).toEqual([{ language: 'de', level: 'B2' }])
  })

  it('removes the row the remove button belongs to', async () => {
    const wrapper = await mountForm({
      profile: profile({
        profile: profileDraft({
          languages: [
            { language: 'fr', level: 'NATIVE' },
            { language: 'en', level: 'C1' },
            { language: 'de', level: 'B1' },
          ],
        }),
      }),
    })

    await wrapper.get('[aria-label="Remove language 2"]').trigger('click')
    await wrapper.get('form').trigger('submit')

    expect(submitted(wrapper).languages).toEqual([
      { language: 'fr', level: 'NATIVE' },
      { language: 'de', level: 'B1' },
    ])
  })
})

describe('ProfileForm · refusals and pending state', () => {
  // `loc` is FastAPI's path to the offending field, so it indexes list items with
  // numbers: ['body', 'languages', 0, 'language'].
  function validation(loc: (string | number)[], msg: string) {
    return new ApiError(422, 'invalid', {
      error: 'validation_failed',
      detail: 'invalid',
      errors: [{ loc, msg }],
    })
  }

  it('shows a refusal as one sentence, in an alert', async () => {
    const wrapper = await mountForm({
      profile: profile(),
      error: new ApiError(503, 'no database', {
        error: 'database_unavailable',
        detail: 'no database',
      }),
    })

    const alert = wrapper.get('.auth-error')
    expect(alert.attributes('role')).toBe('alert')
    expect(alert.text()).toBe('The service is temporarily unavailable. Try again in a moment.')
  })

  it('puts a validation message beside the field it names', async () => {
    const wrapper = await mountForm({
      profile: profile(),
      error: validation(['body', 'display_name'], 'String should have at least 1 character'),
    })

    const field = wrapper.get('.auth-field-error')
    expect(field.text()).toBe('String should have at least 1 character')
    // The field message is not the only thing shown: the summary above the form is
    // what a screen reader announces, and the two say different things.
    expect(wrapper.get('.auth-error').text()).toBe('Some of the details below are not valid.')
  })

  it('attributes a language error to the language fieldset', async () => {
    const wrapper = await mountForm({
      profile: profile(),
      error: validation(['body', 'languages', 0, 'language'], 'not a language code'),
    })

    expect(wrapper.get('.acct-fieldset .auth-field-error').text()).toBe('not a language code')
  })

  it('locks the form and says so while a save is in flight', async () => {
    const wrapper = await mountForm({ profile: profile(), pending: true })

    expect(wrapper.get(SUBMIT).text()).toBe('Saving…')
    expect(wrapper.get(SUBMIT).attributes('disabled')).toBeDefined()
    for (const input of wrapper.findAll('input, select')) {
      expect(input.attributes('disabled')).toBeDefined()
    }
  })

  // /onboarding says "Save and continue", /profile says "Save profile": the same
  // write, but one of them is a step in a sequence.
  it('uses the button label its page gives it', async () => {
    const wrapper = await mountForm({ profile: profile(), submitLabel: 'Save and continue' })
    expect(wrapper.get(SUBMIT).text()).toBe('Save and continue')
  })
})
