// Ported from webapp/src/components/AppShell.test.tsx and
// webapp/src/theme/useTheme.test.ts.
//
// The two V1 files merge here because the shell is where the theme lives now: V1
// had a `useTheme` hook of its own, and the port hands that job to
// @nuxtjs/color-mode, configured in nuxt.config.ts to keep V1's contract —
// localStorage key "theme", `data-theme` on <html>, dark by default. So the hook's
// three cases are asserted through the button that drives them rather than against
// a hook that no longer exists.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import DefaultLayout from '~/layouts/default.vue'
import { useSessionStore } from '~/stores/session'
import { stubFetch } from '../support/http'
import { signedIn } from '../support/v2-fixtures'
import type { Route } from '../support/http'

/**
 * The shell's own requests: the copilot's history, and — since Phase 4 — the
 * session behind the account affordance. 401 is the default because an anonymous
 * visitor is a supported state on these pages (V1's API has no accounts).
 *
 * `extra` comes first: `stubFetch` answers from the first matching route, so a
 * route passed in has to precede the default it replaces.
 */
function shellRoutes(...extra: Route[]): Route[] {
  return [
    ...extra,
    { match: '/api/chat/history', json: { messages: [] } },
    { match: '/api/v2/auth/session', status: 401, json: { error: 'not_authenticated' } },
  ]
}

/** The nav links only. The topbar's account link is an anchor too. */
const NAV = '.nav-link'

function mountShell() {
  return mountSuspended(DefaultLayout, { slots: { default: () => 'content' } })
}

describe('default layout (V1 AppShell)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    // The store outlives a test — it is the app's, not the wrapper's — so an
    // authenticated case would otherwise leak into the next mount.
    useSessionStore().$reset()
    stubFetch(shellRoutes())
  })

  it('renders nav links and content', async () => {
    const wrapper = await mountShell()

    const links = wrapper.findAll(NAV).map(a => a.text())
    expect(links).toEqual(['Overview', 'Jobs', 'Analytics', 'Settings'])
    expect(wrapper.text()).toContain('content')
    expect(wrapper.text()).toContain('⌘ Command Center')
  })

  it('points each nav link at its route', async () => {
    const wrapper = await mountShell()
    expect(wrapper.findAll(NAV).map(a => a.attributes('href')))
      .toEqual(['/', '/jobs', '/analytics', '/settings'])
  })

  // The V1 pages this shell wraps work without an account, so an anonymous
  // visitor gets an invitation rather than a redirect (app/middleware/auth.ts).
  it('offers a sign-in link when there is no session', async () => {
    const wrapper = await mountShell()
    await flushPromises()

    const account = wrapper.get('.topbar-account')
    expect(account.text()).toBe('Sign in')
    expect(account.attributes('href')).toBe('/login')
  })

  it('links to the account once a session is known', async () => {
    stubFetch(shellRoutes({ match: '/api/v2/auth/session', json: signedIn() }))
    const wrapper = await mountShell()
    await flushPromises()

    const account = wrapper.get('.topbar-account')
    expect(account.text()).toBe('Test Candidate')
    expect(account.attributes('href')).toBe('/profile')
  })

  it('toggles the global copilot panel', async () => {
    const wrapper = await mountShell()

    expect(wrapper.find('[aria-label="Copilot"]').exists()).toBe(false)
    const copilotButton = wrapper.findAll('button').find(b => /copilot/i.test(b.text()))!
    await copilotButton.trigger('click')
    await flushPromises()

    // V1 asserted `getByRole("region", { name: /copilot/i })`, which matched
    // CopilotPanel's own <section aria-label="Copilot"> — not the dock <aside>.
    // `find`, not `get`: the assertion is the presence itself, and `get` drops
    // `exists()` from the wrapper it returns because it throws instead.
    expect(wrapper.find('section[aria-label="Copilot"]').exists()).toBe(true)

    await wrapper.get('.copilot-dock-head button').trigger('click')
    expect(wrapper.find('section[aria-label="Copilot"]').exists()).toBe(false)
  })
})

describe('theme toggle (V1 useTheme)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    useSessionStore().$reset()
    stubFetch(shellRoutes())
  })

  // V1's "defaults to dark" and "reads persisted theme on init" are NOT asserted
  // here, and not because they stopped mattering. @nuxtjs/color-mode decides the
  // initial value from an inline script that runs before paint, and its client
  // plugin substitutes a hardcoded light stub whenever `import.meta.test` is set
  // and that script has not run — so in this environment the starting value is the
  // module's test stub, not the app's configuration. Both cases are covered in
  // tests/e2e/shell.spec.ts, in a real browser where that script executes, which
  // is the only place the answer is real.

  it('writes data-theme on <html> and persists under V1\'s storage key', async () => {
    const wrapper = await mountShell()
    const before = useColorMode().value

    await wrapper.get('[aria-label="Toggle theme"]').trigger('click')
    await flushPromises()

    const after = useColorMode().value
    expect(after).not.toBe(before)
    // "theme", not the module default "nuxt-color-mode": a V1 user's saved choice
    // has to survive the migration.
    expect(localStorage.getItem('theme')).toBe(after)
    expect(document.documentElement.getAttribute('data-theme')).toBe(after)
  })

  it('labels the button for the mode currently applied', async () => {
    const wrapper = await mountShell()
    const toggle = wrapper.get('[aria-label="Toggle theme"]')
    const label = () => toggle.text()

    const first = useColorMode().value
    expect(label()).toBe(first === 'dark' ? '☾ Dark' : '☀ Light')

    await toggle.trigger('click')
    await flushPromises()
    const second = useColorMode().value
    expect(label()).toBe(second === 'dark' ? '☾ Dark' : '☀ Light')
  })

  it('toggles back to the mode it started in', async () => {
    const wrapper = await mountShell()
    const toggle = wrapper.get('[aria-label="Toggle theme"]')
    const before = useColorMode().value

    await toggle.trigger('click')
    await flushPromises()
    await toggle.trigger('click')
    await flushPromises()

    expect(useColorMode().value).toBe(before)
    expect(localStorage.getItem('theme')).toBe(before)
    expect(document.documentElement.getAttribute('data-theme')).toBe(before)
  })
})
