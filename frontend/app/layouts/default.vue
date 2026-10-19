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

  The shell now treats V2 as the product surface. Legacy V1 routes remain available
  directly while migration work continues, but must not be primary navigation: they
  read the separate V1 store and can make an otherwise populated V2 catalog look empty.
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useSessionStore } from '~/stores/session'
import { useUiStore } from '~/stores/ui'
import { useLocale } from '~/composables/useLocale'

/**
 * These routes all consume `/api/v2` data. Keep this as the sole primary menu so a
 * signed-in candidate always lands on the PostgreSQL/PostGIS workflow, not its
 * legacy V1 compatibility screens.
 */
const PRIMARY_LINKS = [
  { to: '/companies', key: 'nav.companies', exact: false },
  { to: '/map', key: 'nav.map', exact: false },
  { to: '/documents', key: 'nav.documents', exact: false },
  { to: '/evidence', key: 'nav.evidence', exact: false },
  { to: '/applications', key: 'nav.applications', exact: false },
  { to: '/interview', key: 'nav.interview', exact: false },
  { to: '/strategy', key: 'nav.strategy', exact: false },
  { to: '/billing', key: 'nav.billing', exact: false },
  { to: '/settings', key: 'nav.settings', exact: false },
]

const colorMode = useColorMode()
const ui = useUiStore()
const session = useSessionStore()
const { locale, t } = useLocale()
const commandOpen = ref(false)
const navigationOpen = ref(false)
const localeItems = [
  { value: 'en', label: 'EN' },
  { value: 'fr', label: 'FR' },
]
useHead({ htmlAttrs: { lang: locale } })

// Every primary screen is session-protected. An anonymous visitor gets the explicit
// account action in the top bar instead of a menu full of links that immediately
// redirect to the login screen.
const links = computed(() => (session.isAuthenticated ? PRIMARY_LINKS : []))
const accountInitials = computed(() => (session.label ?? '')
  .split(/\s+/)
  .filter(Boolean)
  .slice(0, 2)
  .map(part => part[0])
  .join('')
  .toUpperCase())
const commandGroups = computed(() => [
  {
    id: 'navigation',
    label: t('command.navigation'),
    items: links.value.map(link => ({ label: t(link.key), to: link.to })),
  },
  {
    id: 'actions',
    label: t('command.actions'),
    items: [
      {
        label: t('command.openCopilot'),
        icon: 'i-lucide-sparkles',
        onSelect: () => { ui.copilotOpen = true },
      },
      {
        label: t('command.toggleTheme'),
        icon: 'i-lucide-sun-moon',
        onSelect: () => toggleTheme(),
      },
    ],
  },
])

// Fire and forget: the affordance renders "Sign in" until the answer lands, which
// is what it would show anyway if there is no session.
onMounted(() => {
  void session.ensure()
  window.addEventListener('keydown', onWindowKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onWindowKeydown)
  document.body.style.overflow = ''
})

watch(navigationOpen, (open) => {
  if (import.meta.client) document.body.style.overflow = open ? 'hidden' : ''
})

function toggleTheme() {
  colorMode.preference = colorMode.value === 'dark' ? 'light' : 'dark'
}

function closeNavigation() {
  navigationOpen.value = false
}

function onWindowKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeNavigation()
}

defineShortcuts({
  meta_k: () => { commandOpen.value = true },
  ctrl_k: () => { commandOpen.value = true },
})
</script>

<template>
  <div class="shell">
    <aside
      id="primary-navigation"
      class="sidebar"
      :class="{ 'sidebar--open': navigationOpen }"
      aria-label="Primary navigation"
    >
      <div class="brand">
        ⌘ Command Center
      </div>
      <NuxtLink
        v-for="link in links"
        :key="link.to"
        :to="link.to"
        class="nav-link"
        :active-class="link.exact ? '' : 'active'"
        :exact-active-class="link.exact ? 'active' : ''"
        @click="closeNavigation"
      >
        {{ t(link.key) }}
      </NuxtLink>
    </aside>
    <button
      v-if="navigationOpen"
      type="button"
      class="nav-backdrop"
      :aria-label="t('common.closeNavigation')"
      @click="closeNavigation"
    />

    <div class="main">
      <div class="topbar">
        <button
          type="button"
          class="mobile-nav-toggle"
          :aria-label="navigationOpen ? t('common.closeNavigation') : t('common.openNavigation')"
          :aria-expanded="navigationOpen"
          aria-controls="primary-navigation"
          @click="navigationOpen = !navigationOpen"
        >
          <UIcon :name="navigationOpen ? 'i-lucide-x' : 'i-lucide-menu'" aria-hidden="true" />
          <span class="sr-only">{{ navigationOpen ? t('common.closeNavigation') : t('common.openNavigation') }}</span>
        </button>
        <button class="command-trigger" type="button" :aria-label="t('command.open')" @click="commandOpen = true">
          <UIcon name="i-lucide-search" aria-hidden="true" />
          <span>{{ t('command.search') }}</span>
          <kbd>⌘K</kbd>
        </button>
        <NuxtLink
          v-if="session.isAuthenticated"
          to="/profile"
          class="topbar-account topbar-account--avatar"
          :aria-label="`${t('page.account')}: ${session.label}`"
        >
          <span class="account-avatar" aria-hidden="true">{{ accountInitials }}</span>
          <span class="sr-only">{{ session.label }}</span>
        </NuxtLink>
        <NuxtLink
          v-else
          to="/login"
          class="topbar-account topbar-account--avatar"
          :aria-label="t('common.signIn')"
        >
          <UIcon name="i-lucide-user" aria-hidden="true" />
          <span class="sr-only">{{ t('common.signIn') }}</span>
        </NuxtLink>
        <label class="locale-switcher">
          <span class="sr-only">{{ t('common.language') }}</span>
          <USelect
            v-model="locale"
            :items="localeItems"
            value-key="value"
            :aria-label="t('common.language')"
          />
        </label>
        <button class="theme-btn" :aria-label="t('common.copilot')" @click="ui.toggleCopilot()">
          <UIcon name="i-lucide-sparkles" aria-hidden="true" />
          <span class="topbar-control-label">{{ t('common.copilot') }}</span>
        </button>
        <button class="theme-btn" :aria-label="t('common.toggleTheme')" @click="toggleTheme">
          <UIcon :name="colorMode.value === 'dark' ? 'i-lucide-moon' : 'i-lucide-sun'" aria-hidden="true" />
          <span class="topbar-control-label">
            {{ colorMode.value === 'dark' ? t('common.dark') : t('common.light') }}
          </span>
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

    <UModal
      v-model:open="commandOpen"
      :title="t('command.search')"
      :description="t('command.placeholder')"
      :close="false"
      :ui="{ content: 'command-modal' }"
    >
      <template #content>
        <UCommandPalette
          :groups="commandGroups"
          :placeholder="t('command.placeholder')"
          class="command-palette"
          @update:model-value="commandOpen = false"
        />
      </template>
    </UModal>
  </div>
</template>
