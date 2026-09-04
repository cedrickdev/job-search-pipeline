// Vitest for the V2 frontend.
//
// `defineVitestConfig` from @nuxt/test-utils is what makes `environment: 'nuxt'`
// available, and that environment is the reason these tests can mount the real
// components: it boots a Nuxt app once per run so auto-imports (`useAsyncData`,
// `useColorMode`, `navigateTo`), the module runtimes (Nuxt UI, Pinia, VueUse) and
// `~/...` aliases resolve exactly as they do in the browser. V1's equivalent was
// `test/providers.tsx`, which had to wrap every render in a QueryClientProvider
// and a MemoryRouter by hand; here the environment provides both.
//
// `domEnvironment: 'happy-dom'` rather than V1's jsdom: it is the Nuxt default,
// it is already a dependency, and nothing in the ported suite needs a jsdom-only
// API. The two places that touch the DOM directly — the theme attribute on
// `<html>` and `MediaRecorder` — work the same in both.
//
// The directory is `tests/nuxt`, not `tests/unit`, and the name is load-bearing:
// `.nuxt/tsconfig.app.json` includes `../tests/nuxt/**/*`, so with that name the
// spec files are part of the same TypeScript program as the app and `nuxt
// typecheck` checks them. Under any other name they type-check nowhere, and a
// prop or payload rename would leave the suite green against types that no longer
// exist. It is also Nuxt's own convention for specs that need this environment —
// which is all of them here.
//
// `include` is restricted to that directory. `tests/e2e` holds Playwright specs
// which import `@playwright/test`; without this Vitest would collect them and
// fail on a `test` function that is not its own.
import { defineVitestConfig } from '@nuxt/test-utils/config'

export default defineVitestConfig({
  test: {
    environment: 'nuxt',
    environmentOptions: {
      nuxt: {
        rootDir: import.meta.dirname,
        domEnvironment: 'happy-dom',
      },
    },
    include: ['tests/nuxt/**/*.spec.ts'],
    // The Nuxt environment builds the app on first use; 5s (Vitest's default) is
    // enough for a test but not always for the first file that triggers that
    // build on a cold cache.
    testTimeout: 20_000,
    hookTimeout: 20_000,
  },
})
