<!--
  Sign in. One of the two screens a visitor with no session can reach.

  `layout: false`, so no application shell: the sidebar links to pages that need an
  account, and a copilot dock on a login form would be an invitation to type into
  something that cannot answer. The page renders on its own.

  Three details are behaviour rather than styling.

  The form posts to `POST /api/v2/auth/login` through the session store, which is
  what puts the account in a place the router can read (stores/session.ts). No token
  is handled here — the reply carries none; the session arrives as two `Set-Cookie`
  headers the browser files away, and the `HttpOnly` half is unreadable to this code
  by design (docs/AUTHENTICATION.md §The two cookies).

  `?redirect=` is honoured but only for a path of this app. A guard that forwarded an
  absolute URL would be an open redirect: an attacker's link to
  `/login?redirect=https://evil.example` would bounce a freshly signed-in user off
  site, and the address bar would have said `/login` the whole way.

  Every refusal is one sentence from `errorMessage`, and the sentence for wrong
  credentials does not say which half was wrong — the API answers identically for an
  unknown address and a bad password, and a more helpful form here would hand back
  the account-enumeration the API is careful not to give
  (docs/AUTHENTICATION.md §Enumeration).
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useSessionStore } from '~/stores/session'
import { errorMessage } from '~/utils/v2-errors'

definePageMeta({ layout: false })

useHead({ title: 'Sign in · Command Center' })

const session = useSessionStore()
const route = useRoute()

const email = ref('')
const password = ref('')
const pending = ref(false)
const failure = ref<unknown>(null)

const message = computed(() => (failure.value === null ? null : errorMessage(failure.value)))

/**
 * Where to go once there is a session.
 *
 * An account that has not finished onboarding goes there regardless of what
 * `?redirect=` asked for: the auth middleware would bounce it straight back, and
 * arriving at a half-empty screen first is worse than not arriving at all.
 */
function destination(): string {
  if (session.needsOnboarding) return '/onboarding'
  const wanted = route.query.redirect
  if (typeof wanted === 'string' && wanted.startsWith('/') && !wanted.startsWith('//')) {
    return wanted
  }
  return '/'
}

// A signed-in browser has no business on this form; it lands here from a bookmark
// or the back button. `ensure()` answers from the store when the shell has already
// asked, so this is normally not a request.
onMounted(async () => {
  if (await session.ensure()) await navigateTo(destination())
})

async function submit() {
  pending.value = true
  failure.value = null
  try {
    await session.signIn({ email: email.value, password: password.value })
    await navigateTo(destination())
  }
  catch (error) {
    failure.value = error
    // Cleared on any refusal, including a lockout: the value in the box is what was
    // just rejected, and leaving it there invites the same submission again.
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
      <h1>Sign in</h1>
      <p class="auth-hint">
        Your saved searches and candidate profile live on this machine, behind your
        account.
      </p>

      <p v-if="message" class="auth-error" role="alert">
        {{ message }}
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

      <label class="auth-field" for="password">Password</label>
      <input
        id="password"
        v-model="password"
        type="password"
        name="password"
        autocomplete="current-password"
        required
        :disabled="pending"
      >

      <button class="btn-primary" type="submit" :disabled="pending">
        {{ pending ? 'Signing in…' : 'Sign in' }}
      </button>

      <p class="auth-alt">
        No account yet?
        <NuxtLink to="/register">
          Create one
        </NuxtLink>
      </p>
    </form>
  </main>
</template>
