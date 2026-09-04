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
import { stubFetch } from '../support/http'

function mountShell() {
  return mountSuspended(DefaultLayout, { slots: { default: () => 'content' } })
}

describe('default layout (V1 AppShell)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    clearNuxtData()
    stubFetch([{ match: '/api/chat/history', json: { messages: [] } }])
  })

  it('renders nav links and content', async () => {
    const wrapper = await mountShell()

    const links = wrapper.findAll('a').map(a => a.text())
    expect(links).toEqual(['Overview', 'Jobs', 'Analytics', 'Settings'])
    expect(wrapper.text()).toContain('content')
    expect(wrapper.text()).toContain('⌘ Command Center')
  })

  it('points each nav link at its route', async () => {
    const wrapper = await mountShell()
    expect(wrapper.findAll('a').map(a => a.attributes('href')))
      .toEqual(['/', '/jobs', '/analytics', '/settings'])
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
    stubFetch([{ match: '/api/chat/history', json: { messages: [] } }])
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
