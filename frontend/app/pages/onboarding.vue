<!--
  Onboarding: the two writes an account needs before the rest of the app means
  anything, and the stamp that ends it.

  **The steps are the server's answer, not this page's state.** `GET
  /api/v2/onboarding` reports `has_profile`, the search counts and `may_complete`,
  and the step shown is derived from those on every load. A wizard holding its own
  step index would disagree with the backend the moment a user reloaded halfway, or
  arrived with a profile already saved from another tab; `POST /onboarding/complete`
  re-reads both facts anyway and answers 409 if they are missing, so anything this
  page decided on its own would only be a second opinion.

  **A paused search is its own case.** The stamp needs an *active* search, so an
  account with one saved-but-paused search has finished the writing and still cannot
  finish onboarding. That is why the state carries two counts rather than a boolean
  (backend/app/services/onboarding.py) — the step-two form loads that search so the
  fix is ticking "Run this search" instead of creating a second one.

  `layout: false`, as on login and register: the shell's sidebar leads to screens
  built around a profile that does not exist yet.
-->
<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  useCompleteOnboarding,
  useOnboardingQuery,
  useProfileQuery,
  useSaveProfile,
  useSearchProfileActions,
  useSearchProfilesQuery,
} from '~/composables/useAccount'
import { useSessionStore } from '~/stores/session'
import type { CandidateProfileDraft, SearchProfileDraft } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'
import { useLocale } from '~/composables/useLocale'
import { countryOptionsFor, useCountryPacksQuery } from '~/composables/useCountryPacks'

definePageMeta({ middleware: 'auth', layout: false })

const session = useSessionStore()
const { t } = useLocale()
useHead({ title: () => `${t('page.onboarding')} · Command Center` })
const { data: state, status } = useOnboardingQuery()
const { data: profile } = useProfileQuery()
const { data: searches } = useSearchProfilesQuery()
const { data: countryPacks } = useCountryPacksQuery()
const countryOptions = computed(() => countryOptionsFor(countryPacks.value))
const saveProfile = useSaveProfile()
const { create, update } = useSearchProfileActions()
const finish = useCompleteOnboarding()
const termsAccepted = ref(false)

const loading = computed(() => status.value === 'pending' && !state.value)
const hasProfile = computed(() => state.value?.has_profile === true)
const activeSearches = computed(() => state.value?.active_search_profiles ?? 0)
const savedSearches = computed(() => state.value?.search_profiles ?? 0)
const mayComplete = computed(() => state.value?.may_complete === true)
const paused = computed(() => savedSearches.value > 0 && activeSearches.value === 0)

/** The step to render: the first unmet requirement, or the finish line. */
const step = computed(() => {
  if (!hasProfile.value) return 1
  if (activeSearches.value === 0) return 2
  return 3
})

// The search step edits the existing one when there is one — see the paused case
// above. `create` and `update` are separate mutations, so their errors and their
// `isPending` flags stay apart.
const existing = computed(() => searches.value?.search_profiles[0] ?? null)
const searchWrite = computed(() => (existing.value === null ? create : update))

const failure = computed(() => finish.error ?? null)
const message = computed(() => (failure.value ? errorMessage(failure.value) : null))

async function onProfile(draft: CandidateProfileDraft) {
  await saveProfile.mutateAsync(draft).catch(() => {})
}

async function onSearch(draft: SearchProfileDraft) {
  const current = existing.value
  const write = current === null
    ? create.mutateAsync(draft)
    : update.mutateAsync({ id: current.id, draft })
  await write.catch(() => {})
}

async function onFinish() {
  try {
    await finish.mutateAsync()
    await navigateTo('/')
  }
  catch {
    // `finish.error` is already rendered; a 409 means the state above is stale and
    // the refreshed counts will move the step back on their own.
  }
}
/** Done, current, or still ahead — the progress list's only logic. */
function stepClass(n: number): string {
  if (step.value > n) return 'onb-step done'
  return step.value === n ? 'onb-step current' : 'onb-step'
}

async function onSignOut() {
  await session.signOut()
  await navigateTo('/login')
}
</script>
<template>
  <main class="auth-page auth-page--wide">
    <section class="auth-card auth-card--wide">
      <h1>{{ t('page.onboarding') }}</h1>
      <p class="auth-hint">
        {{ t('onboarding.intro') }}
      </p>

      <p v-if="loading" class="ov-state">
        Loading…
      </p>

      <template v-else>
        <ol class="onb-steps">
          <li :class="stepClass(1)">
            {{ t('onboarding.profileStep') }}
          </li>
          <li :class="stepClass(2)">
            {{ t('onboarding.searchStep') }}
          </li>
          <li :class="stepClass(3)">
            {{ t('onboarding.finishStep') }}
          </li>
        </ol>

        <section v-if="step === 1" class="onb-panel">
          <h2>{{ t('onboarding.who') }}</h2>
          <p class="auth-hint">{{ t('onboarding.whoHelp') }}</p>
          <ProfileForm
            :profile="profile ?? null"
            :pending="saveProfile.isPending"
            :error="saveProfile.error"
            :submit-label="t('onboarding.saveContinue')"
            :country-options="countryOptions"
            @submit="onProfile"
          />
          <ReferenceCvImport />
        </section>

        <section v-else-if="step === 2" class="onb-panel">
          <h2>{{ t('onboarding.lookingFor') }}</h2>
          <p v-if="paused" class="auth-hint">
            {{ t('onboarding.pausedHelp') }}
          </p>
          <p v-else class="auth-hint">
            {{ t('onboarding.searchHelp') }}
          </p>
          <SearchProfileForm
            :search="existing"
            :pending="searchWrite.isPending"
            :error="searchWrite.error"
            :submit-label="t('onboarding.saveContinue')"
            :country-options="countryOptions"
            @submit="onSearch"
          />
        </section>

        <section v-else class="onb-panel">
          <h2>{{ t('onboarding.ready') }}</h2>
          <p class="auth-hint">{{ t('onboarding.readyHelp') }}</p>
          <p v-if="message" class="auth-error" role="alert">
            {{ message }}
          </p>
          <label class="terms-consent">
            <input v-model="termsAccepted" type="checkbox" data-test="terms-consent">
            <span>
              {{ t('onboarding.terms') }}
              <NuxtLink to="/terms" target="_blank">{{ t('onboarding.termsLink') }}</NuxtLink>
            </span>
          </label>
          <p v-if="finish.error && !termsAccepted" class="auth-field-error" role="alert">
            {{ t('onboarding.termsRequired') }}
          </p>
          <div class="acct-actions">
            <button
              class="btn-primary"
              type="button"
              :disabled="!mayComplete || !termsAccepted || finish.isPending"
              @click="onFinish"
            >
              {{ finish.isPending ? t('onboarding.finishing') : t('onboarding.finish') }}
            </button>
          </div>
        </section>

        <p class="auth-alt">
          {{ t('onboarding.signedInAs') }} {{ session.label }} ·
          <button class="btn-link" type="button" @click="onSignOut">
            Sign out
          </button>
        </p>
      </template>
    </section>
  </main>
</template>
