<!--
  The candidate profile form, shared by /onboarding and /profile.

  One component for both because they are the same write: `PUT /api/v2/me/profile`
  creates or replaces, and the account has exactly one profile whose id is derived
  from the account. Two forms would be two chances for the onboarding version to
  drift from the editing version.

  **The draft is merged over the loaded one, not built from the inputs alone.** The
  `PUT` replaces the whole profile, so a form that sent only the fields it renders
  would silently erase the ones it does not: `work_authorizations` and `availability`
  are part of the profile, have no editor until a later phase, and would be dropped
  on the first save from this screen. Spreading the loaded draft first is what keeps
  them. It is the same hazard settings.vue documents, one level down.

  **`base_location` is `null` when both boxes are empty.** A `Location` has to locate
  something — the domain rejects one with every field unset — so an empty city and an
  empty country mean "no location given", not "a location that says nothing".

  Country and language are typed as codes rather than picked from a list. The lists
  are Country Pack knowledge (Phase 5); until they exist, a two-letter code with the
  format spelled out beside it is honest, and the server's 422 is what has the final
  say either way.
-->
<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { CandidateProfile, CandidateProfileDraft, LanguageLevel, Location } from '~/types/v2'
import { errorMessage, fieldErrors } from '~/utils/v2-errors'
import { useLocale } from '~/composables/useLocale'
import { DEFAULT_COUNTRY_OPTIONS } from '~/composables/useCountryPacks'
import type { CountryOption } from '~/composables/useCountryPacks'

const LEVELS: LanguageLevel[] = ['A1', 'A2', 'B1', 'B2', 'C1', 'C2', 'NATIVE']

const props = withDefaults(defineProps<{
  /** The saved profile, or null before onboarding has written one. */
  profile: CandidateProfile | null
  pending?: boolean
  error?: unknown
  submitLabel?: string
  /** /profile is read-first; onboarding and edit mode pass true or omit this. */
  editable?: boolean
  /** Fall back to the authenticated email when the profile contact is incomplete. */
  accountEmail?: string | null
  countryOptions?: readonly CountryOption[]
}>(), {
  editable: true,
  accountEmail: null,
  countryOptions: () => DEFAULT_COUNTRY_OPTIONS,
})

const emit = defineEmits<{ submit: [draft: CandidateProfileDraft] }>()
const { locale, t } = useLocale()

interface LanguageRow { language: string, level: LanguageLevel }

const displayName = ref('')
const email = ref('')
const phone = ref('')
const headline = ref('')
const city = ref('')
const country = ref('')
const languages = ref<LanguageRow[]>([])

// Copied out of the response rather than bound to it. `v-model` straight onto
// `props.profile` would write half-typed input into the query cache, where every
// other reader of that payload would see it — and mutating a prop is Vue's own
// warning besides. A newly loaded profile re-seeds the boxes, which is what a save's
// refetch should do; nothing else refetches this key.
watch([() => props.profile, () => props.accountEmail], ([next]) => {
  const saved = next?.profile
  displayName.value = saved?.display_name ?? ''
  email.value = saved?.contact?.email ?? props.accountEmail ?? ''
  phone.value = saved?.contact?.phone ?? ''
  headline.value = saved?.headline ?? ''
  city.value = saved?.base_location?.city ?? ''
  country.value = saved?.base_location?.country ?? ''
  languages.value = (saved?.languages ?? []).map(entry => ({ ...entry }))
}, { immediate: true })

const message = computed(() => (props.error ? errorMessage(props.error) : null))
const fields = computed(() => fieldErrors(props.error))
const editable = computed(() => props.editable !== false)
const disabled = computed(() => Boolean(props.pending) || !editable.value)
const canSubmit = computed(() => editable.value && displayName.value.trim() !== '' && !props.pending)

function addLanguage() {
  languages.value.push({ language: '', level: 'B2' })
}

function removeLanguage(index: number) {
  languages.value.splice(index, 1)
}

function submit() {
  const saved = props.profile?.profile
  const base = saved?.base_location
  // A user-typed location is `SOURCE_PROVIDED` at `UNKNOWN` precision — the honest
  // provenance for an address nobody geocoded (§7). An edited saved location keeps
  // whatever provenance it already had, along with the point the form never shows,
  // so a radius search still resolves against it (Phase 8 re-geocodes on demand).
  const location: Location | null = city.value === '' && country.value === ''
    ? null
    : {
        ...base,
        city: city.value === '' ? null : city.value,
        country: country.value === '' ? null : country.value.toUpperCase(),
        provenance: base?.provenance ?? 'SOURCE_PROVIDED',
        precision: base?.precision ?? 'UNKNOWN',
      }
  emit('submit', {
    // Everything this form does not render, kept as it was saved.
    work_authorizations: saved?.work_authorizations ?? [],
    availability: saved?.availability ?? null,
    display_name: displayName.value.trim(),
    contact: email.value.trim() === '' ? null : {
      email: email.value.trim(),
      phone: phone.value.trim() === '' ? null : phone.value.trim(),
    },
    headline: headline.value === '' ? null : headline.value,
    base_location: location,
    languages: languages.value
      .filter(row => row.language !== '')
      .map(row => ({ language: row.language.toLowerCase(), level: row.level })),
  })
}
</script>

<template>
  <form class="acct-form" novalidate @submit.prevent="submit">
    <p v-if="message" class="auth-error" role="alert">
      {{ message }}
    </p>

    <label class="acct-field" for="profile-display-name">{{ t('profile.name') }}</label>
    <input
      id="profile-display-name"
      v-model="displayName"
      type="text"
      autocomplete="name"
      required
      :disabled="disabled"
    >
    <p v-if="fields.display_name" class="auth-field-error" role="alert">
      {{ fields.display_name }}
    </p>

    <label class="acct-field" for="profile-headline">
      {{ t('profile.headline') }} <span class="auth-optional">({{ t('auth.optional') }})</span>
    </label>
    <input
      id="profile-headline"
      v-model.trim="headline"
      type="text"
      :placeholder="locale === 'fr' ? 'Ingénieur backend' : 'Backend engineer'"
      :disabled="disabled"
    >

    <div class="acct-row">
      <div>
        <label class="acct-field" for="profile-email">{{ t('profile.email') }}</label>
        <input
          id="profile-email"
          v-model.trim="email"
          type="email"
          autocomplete="email"
          required
          :disabled="disabled"
        >
      </div>
      <div>
        <label class="acct-field" for="profile-phone">
          {{ t('profile.phone') }} <span class="auth-optional">({{ t('auth.optional') }})</span>
        </label>
        <input
          id="profile-phone"
          v-model.trim="phone"
          type="tel"
          autocomplete="tel"
          :disabled="disabled"
        >
      </div>
    </div>

    <div class="acct-row">
      <div>
        <label class="acct-field" for="profile-city">{{ t('profile.city') }}</label>
        <input id="profile-city" v-model.trim="city" type="text" :disabled="disabled">
      </div>
      <div>
        <label class="acct-field" for="profile-country">{{ t('profile.country') }}</label>
        <select
          id="profile-country"
          v-model="country"
          :disabled="disabled"
          aria-describedby="profile-country-hint"
        >
          <option value="">—</option>
          <option v-for="option in countryOptions" :key="option.value" :value="option.value">
            {{ option.label }}
          </option>
        </select>
      </div>
    </div>
    <p id="profile-country-hint" class="auth-hint">
      {{ t('profile.countryHint') }}
    </p>
    <p v-if="fields.base_location || fields.country" class="auth-field-error" role="alert">
      {{ fields.base_location ?? fields.country }}
    </p>

    <fieldset class="acct-fieldset">
      <legend>{{ t('profile.languages') }}</legend>
      <p class="auth-hint">{{ t('profile.languageHelp') }}</p>
      <div v-for="(row, index) in languages" :key="index" class="acct-row acct-row--list">
        <input
          v-model.trim="row.language"
          type="text"
          maxlength="2"
          placeholder="fr"
          pattern="[A-Za-z]{2}"
          :aria-label="`Language ${index + 1} code`"
          :disabled="disabled"
        >
        <select
          v-model="row.level"
          :aria-label="`Language ${index + 1} level`"
          :disabled="disabled"
        >
          <option v-for="level in LEVELS" :key="level" :value="level">
            {{ level }}
          </option>
        </select>
        <button
          v-if="editable"
          class="btn-ghost"
          type="button"
          :aria-label="`Remove language ${index + 1}`"
          :disabled="disabled"
          @click="removeLanguage(index)"
        >
          ✕
        </button>
      </div>
      <p v-if="fields.languages || fields.language" class="auth-field-error" role="alert">
        {{ fields.languages ?? fields.language }}
      </p>
      <button v-if="editable" class="btn-ghost" type="button" :disabled="pending" @click="addLanguage">
        + Add a language
      </button>
    </fieldset>

    <div v-if="editable" class="acct-actions">
      <button class="btn-primary" type="submit" :disabled="!canSubmit">
        {{ pending ? t('profile.saving') : (submitLabel ?? t('profile.save')) }}
      </button>
    </div>
  </form>
</template>
