<!--
  Create an account. The other screen a visitor with no session can reach.

  `POST /api/v2/auth/register` answers 201 with the same body a login returns and
  sets the same two cookies, so a new account is signed in the moment it exists —
  there is no "now sign in with your new password" step, and nothing here has to
  handle a second flow.

  The password rule is length only: twelve characters minimum, no composition
  requirement, and the field is not repeated in a "confirm" box. That is the
  backend's policy (backend/app/core/passwords.py, following NIST SP 800-63B) and
  the form states it rather than inventing a stricter one — a client-side rule the
  server does not share is a rule that gets forgotten by the next client.

  The minimum is asserted here *and* server-side, on purpose: this one gives
  immediate feedback, and that one is what a future CLI cannot bypass. The
  `minlength` attribute is advisory in the same way; the 422 is the real answer, and
  `fieldErrors` is what puts it next to the field it belongs to.
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useSessionStore } from '~/stores/session'
import { errorMessage, fieldErrors } from '~/utils/v2-errors'

definePageMeta({ layout: false })

useHead({ title: 'Create an account · Command Center' })

/** Matches `MINIMUM_PASSWORD_LENGTH` in backend/app/core/passwords.py. */
const MINIMUM_PASSWORD_LENGTH = 12

const session = useSessionStore()

const displayName = ref('')
const email = ref('')
const password = ref('')
const pending = ref(false)
const failure = ref<unknown>(null)

const message = computed(() => (failure.value === null ? null : errorMessage(failure.value)))
const fields = computed(() => fieldErrors(failure.value))
const tooShort = computed(() =>
  password.value.length > 0 && password.value.length < MINIMUM_PASSWORD_LENGTH)

onMounted(async () => {
  if (await session.ensure()) await navigateTo(session.needsOnboarding ? '/onboarding' : '/')
})

async function submit() {
  pending.value = true
  failure.value = null
  try {
    await session.signUp({
      email: email.value,
      password: password.value,
      // Empty is not a name. The field is optional server-side, and `null` is how
      // "not given" is spelled there; `""` would fail its `min_length=1`.
      display_name: displayName.value === '' ? null : displayName.value,
    })
    // A brand-new account has no profile and no saved search, so there is exactly
    // one useful destination.
    await navigateTo('/onboarding')
  }
  catch (error) {
    failure.value = error
    password.value = ''
  }
  finally {
    pending.value = false
  }
}
</script>

<template>
  <main class="auth-page">
    <form class="auth-card" novalidate @submit.prevent="submit">
      <h1>Create an account</h1>
      <p class="auth-hint">
        One account per person on this machine. Nothing is sent anywhere: the account
        lives in the database this application runs against.
      </p>

      <p v-if="message" class="auth-error" role="alert">
        {{ message }}
      </p>

      <label class="auth-field" for="display-name">Name <span class="auth-optional">(optional)</span></label>
      <input
        id="display-name"
        v-model.trim="displayName"
        type="text"
        name="display_name"
        autocomplete="name"
        :disabled="pending"
      >
      <p v-if="fields.display_name" class="auth-field-error" role="alert">
        {{ fields.display_name }}
      </p>

      <label class="auth-field" for="email">Email</label>
      <input
        id="email"
        v-model.trim="email"
        type="email"
        name="email"
        autocomplete="email"
        required
        :disabled="pending"
      >
      <p v-if="fields.email" class="auth-field-error" role="alert">
        {{ fields.email }}
      </p>

      <label class="auth-field" for="password">Password</label>
      <input
        id="password"
        v-model="password"
        type="password"
        name="password"
        autocomplete="new-password"
        :minlength="MINIMUM_PASSWORD_LENGTH"
        required
        :disabled="pending"
        aria-describedby="password-hint"
      >
      <p id="password-hint" class="auth-hint">
        At least {{ MINIMUM_PASSWORD_LENGTH }} characters. A passphrase of a few words
        is easier to remember and harder to guess than a short mixed-case password.
      </p>
      <p v-if="tooShort" class="auth-field-error" role="alert">
        {{ MINIMUM_PASSWORD_LENGTH }} characters minimum.
      </p>
      <p v-else-if="fields.password" class="auth-field-error" role="alert">
        {{ fields.password }}
      </p>

      <button class="btn-primary" type="submit" :disabled="pending || tooShort">
        {{ pending ? 'Creating…' : 'Create account' }}
      </button>

      <p class="auth-alt">
        Already have an account?
        <NuxtLink to="/login">
          Sign in
        </NuxtLink>
      </p>
    </form>
  </main>
</template>
