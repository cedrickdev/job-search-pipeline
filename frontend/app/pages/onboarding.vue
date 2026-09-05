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
import { computed } from 'vue'
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

definePageMeta({ middleware: 'auth', layout: false })

useHead({ title: 'Set up your account · Command Center' })

const session = useSessionStore()
const { data: state, status } = useOnboardingQuery()
const { data: profile } = useProfileQuery()
const { data: searches } = useSearchProfilesQuery()
const saveProfile = useSaveProfile()
const { create, update } = useSearchProfileActions()
const finish = useCompleteOnboarding()

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
      <h1>Set up your account</h1>
      <p class="auth-hint">
        Two things, and you are done: who you are, and what you are looking for.
        Both can be changed afterwards from your profile.
      </p>

      <p v-if="loading" class="ov-state">
        Loading…
      </p>

      <template v-else>
        <ol class="onb-steps">
          <li :class="stepClass(1)">
            Your profile
          </li>
          <li :class="stepClass(2)">
            Your first search
          </li>
          <li :class="stepClass(3)">
            Finish
          </li>
        </ol>

        <section v-if="step === 1" class="onb-panel">
          <h2>Who you are</h2>
          <p class="auth-hint">
            Only your name is required. The rest is what postings are matched
            against, so anything you add here narrows what you will be shown.
          </p>
          <ProfileForm
            :profile="profile ?? null"
            :pending="saveProfile.isPending"
            :error="saveProfile.error"
            submit-label="Save and continue"
            @submit="onProfile"
          />
        </section>

        <section v-else-if="step === 2" class="onb-panel">
          <h2>What you are looking for</h2>
          <p v-if="paused" class="auth-hint">
            You have a saved search, but it is paused — tick “Run this search” to
            finish setting up.
          </p>
          <p v-else class="auth-hint">
            One search is enough to start. You can add others, and pause any of them,
            from your profile.
          </p>
          <SearchProfileForm
            :search="existing"
            :pending="searchWrite.isPending"
            :error="searchWrite.error"
            submit-label="Save and continue"
            @submit="onSearch"
          />
        </section>

        <section v-else class="onb-panel">
          <h2>Ready</h2>
          <p class="auth-hint">
            Your profile is saved and one search is active. Finishing takes you to the
            command center.
          </p>
          <p v-if="message" class="auth-error" role="alert">
            {{ message }}
          </p>
          <div class="acct-actions">
            <button
              class="btn-primary"
              type="button"
              :disabled="!mayComplete || finish.isPending"
              @click="onFinish"
            >
              {{ finish.isPending ? 'Finishing…' : 'Finish setup' }}
            </button>
          </div>
        </section>

        <p class="auth-alt">
          Signed in as {{ session.label }} ·
          <button class="btn-link" type="button" @click="onSignOut">
            Sign out
          </button>
        </p>
      </template>
    </section>
  </main>
</template>
