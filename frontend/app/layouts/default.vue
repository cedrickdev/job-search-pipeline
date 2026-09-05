<!--
  The application shell, ported from webapp/src/components/AppShell.tsx.

  In React this was a component that took `children`; in Nuxt the same job is a
  layout, so the markup moves here and `<slot />` replaces `children`. The nav is
  `NuxtLink`, whose `active-class` replaces V1's `NavLink` render-prop — with
  `exact-active-class` on the root link standing in for React Router's `end`.

  The theme toggle no longer owns its state: `useColorMode()` persists the
  preference under the same localStorage key V1 used ("theme") and writes both
  `data-theme` (for the ported tokens) and `.dark`/`.light` (for Nuxt UI). The
  button's label logic is V1's, unchanged.

  The job drawer is NOT mounted here: in V1 only JobsPage rendered it, and the
  overview's rows navigate to /jobs rather than opening it. Hoisting it into the
  shell would be a behaviour change.

  Phase 4 added the account affordance in the topbar, and it is deliberately the
  only account-aware thing in the shell. `session.ensure()` runs on mount — once per
  page load, resolved from the store afterwards — and a 401 is an ordinary answer
  here rather than a failure: the V1 pages this shell wraps have no accounts, so an
  anonymous visitor is a supported state and gets a "Sign in" link instead of a
  redirect. The nav is unchanged for the same reason (app/middleware/auth.ts).
-->
<script setup lang="ts">
import { onMounted } from 'vue'
import { useSessionStore } from '~/stores/session'
import { useUiStore } from '~/stores/ui'

const LINKS = [
  { to: '/', label: 'Overview', exact: true },
  { to: '/jobs', label: 'Jobs', exact: false },
  { to: '/analytics', label: 'Analytics', exact: false },
  { to: '/settings', label: 'Settings', exact: false },
]

const colorMode = useColorMode()
const ui = useUiStore()
const session = useSessionStore()

// Fire and forget: the affordance renders "Sign in" until the answer lands, which
// is what it would show anyway if there is no session.
onMounted(() => {
  void session.ensure()
})

function toggleTheme() {
  colorMode.preference = colorMode.value === 'dark' ? 'light' : 'dark'
}
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        ⌘ Command Center
      </div>
      <NuxtLink
        v-for="link in LINKS"
        :key="link.to"
        :to="link.to"
        class="nav-link"
        :active-class="link.exact ? '' : 'active'"
        :exact-active-class="link.exact ? 'active' : ''"
      >
        {{ link.label }}
      </NuxtLink>
    </aside>

    <div class="main">
      <div class="topbar">
        <NuxtLink v-if="session.isAuthenticated" to="/profile" class="topbar-account">
          {{ session.label }}
        </NuxtLink>
        <NuxtLink v-else to="/login" class="topbar-account">
          Sign in
        </NuxtLink>
        <button class="theme-btn" @click="ui.toggleCopilot()">
          ✦ Copilot
        </button>
        <button class="theme-btn" aria-label="Toggle theme" @click="toggleTheme">
          {{ colorMode.value === 'dark' ? '☾ Dark' : '☀ Light' }}
        </button>
      </div>
      <slot />
    </div>

    <aside v-if="ui.copilotOpen" class="copilot-dock">
      <div class="copilot-dock-head">
        <span>Copilot</span>
        <button class="btn-ghost" @click="ui.closeCopilot()">
          ✕
        </button>
      </div>
      <CopilotPanel scope="global" />
    </aside>
  </div>
</template>
