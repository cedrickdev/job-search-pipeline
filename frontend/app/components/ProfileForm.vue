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

const LEVELS: LanguageLevel[] = ['A1', 'A2', 'B1', 'B2', 'C1', 'C2', 'NATIVE']

const props = defineProps<{
  /** The saved profile, or null before onboarding has written one. */
  profile: CandidateProfile | null
  pending?: boolean
  error?: unknown
  submitLabel?: string
}>()

const emit = defineEmits<{ submit: [draft: CandidateProfileDraft] }>()

interface LanguageRow { language: string, level: LanguageLevel }

const displayName = ref('')
const headline = ref('')
const city = ref('')
const country = ref('')
const languages = ref<LanguageRow[]>([])

// Copied out of the response rather than bound to it. `v-model` straight onto
// `props.profile` would write half-typed input into the query cache, where every
// other reader of that payload would see it — and mutating a prop is Vue's own
// warning besides. A newly loaded profile re-seeds the boxes, which is what a save's
// refetch should do; nothing else refetches this key.
watch(() => props.profile, (next) => {
  const saved = next?.profile
  displayName.value = saved?.display_name ?? ''
  headline.value = saved?.headline ?? ''
  city.value = saved?.base_location?.city ?? ''
  country.value = saved?.base_location?.country ?? ''
  languages.value = (saved?.languages ?? []).map(entry => ({ ...entry }))
}, { immediate: true })

const message = computed(() => (props.error ? errorMessage(props.error) : null))
const fields = computed(() => fieldErrors(props.error))
const canSubmit = computed(() => displayName.value.trim() !== '' && !props.pending)

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

    <label class="acct-field" for="profile-display-name">Name</label>
    <input
      id="profile-display-name"
      v-model="displayName"
      type="text"
      autocomplete="name"
      required
      :disabled="pending"
    >
    <p v-if="fields.display_name" class="auth-field-error" role="alert">
      {{ fields.display_name }}
    </p>

    <label class="acct-field" for="profile-headline">
      Headline <span class="auth-optional">(optional)</span>
    </label>
    <input
      id="profile-headline"
      v-model.trim="headline"
      type="text"
      placeholder="Backend engineer"
      :disabled="pending"
    >

    <div class="acct-row">
      <div>
        <label class="acct-field" for="profile-city">City</label>
        <input id="profile-city" v-model.trim="city" type="text" :disabled="pending">
      </div>
      <div>
        <label class="acct-field" for="profile-country">Country</label>
        <input
          id="profile-country"
          v-model.trim="country"
          type="text"
          maxlength="2"
          placeholder="CH"
          pattern="[A-Za-z]{2}"
          :disabled="pending"
          aria-describedby="profile-country-hint"
        >
      </div>
    </div>
    <p id="profile-country-hint" class="auth-hint">
      Two-letter country code (ISO 3166-1), e.g. CH for Switzerland.
    </p>
    <p v-if="fields.base_location || fields.country" class="auth-field-error" role="alert">
      {{ fields.base_location ?? fields.country }}
    </p>

    <fieldset class="acct-fieldset">
      <legend>Languages</legend>
      <p class="auth-hint">
        The languages you actually speak, at the level you speak them (CEFR, or native).
        Postings are filtered against these.
      </p>
      <div v-for="(row, index) in languages" :key="index" class="acct-row acct-row--list">
        <input
          v-model.trim="row.language"
          type="text"
          maxlength="2"
          placeholder="fr"
          pattern="[A-Za-z]{2}"
          :aria-label="`Language ${index + 1} code`"
          :disabled="pending"
        >
        <select
          v-model="row.level"
          :aria-label="`Language ${index + 1} level`"
          :disabled="pending"
        >
          <option v-for="level in LEVELS" :key="level" :value="level">
            {{ level }}
          </option>
        </select>
        <button
          class="btn-ghost"
          type="button"
          :aria-label="`Remove language ${index + 1}`"
          :disabled="pending"
          @click="removeLanguage(index)"
        >
          ✕
        </button>
      </div>
      <p v-if="fields.languages || fields.language" class="auth-field-error" role="alert">
        {{ fields.languages ?? fields.language }}
      </p>
      <button class="btn-ghost" type="button" :disabled="pending" @click="addLanguage">
        + Add a language
      </button>
    </fieldset>

    <div class="acct-actions">
      <button class="btn-primary" type="submit" :disabled="!canSubmit">
        {{ pending ? 'Saving…' : (submitLabel ?? 'Save profile') }}
      </button>
    </div>
  </form>
</template>
