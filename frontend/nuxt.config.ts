// Nuxt 4 configuration for the V2 frontend.
//
// Two decisions are worth stating here rather than in a commit message.
//
// `ssr: false`. FastAPI is the backend and the source of truth for business
// behaviour (docs/FRONTEND_ARCHITECTURE.md), and the V1 frontend it replaces was
// a browser-only SPA served by that same FastAPI process. Rendering on the
// server would put a second runtime between the browser and the API — one that
// would need its own base URL, its own view of authentication (Phase 4) and its
// own error handling. Turning SSR off makes "no domain logic in Nitro" a
// property of the build instead of a rule people have to remember, and it keeps
// the deployment story identical to V1's: a static bundle plus one API.
//
// The `/api` dev proxy. `nuxt dev` serves the app on 3000 while FastAPI listens
// on 8765, so every relative `/api/...` call would 404 in development. Routing
// them through Nitro's dev proxy keeps every request path in the code relative,
// exactly as the React app had them, which is what makes the same code work
// unchanged behind FastAPI's own static mount in production.
//
// `buildAssetsDir` and the font prefix. server/app.py mounts exactly one static
// directory — `<spa_dist>/assets` at `/assets` — and falls back to index.html for
// anything else, so a bundle under Nuxt's default `/_nuxt/` would be answered
// with HTML. Pointing both the build assets and the self-hosted fonts inside
// `/assets/` lets `nuxt generate` drop a bundle the existing backend serves
// as-is, with no change to the FastAPI mount.
export default defineNuxtConfig({
  compatibilityDate: '2025-09-01',

  modules: ['@nuxt/ui', '@pinia/nuxt', '@vueuse/nuxt'],

  css: ['~/assets/css/main.css'],

  app: { buildAssetsDir: '/assets/' },

  fonts: { assets: { prefix: '/assets/fonts/' } },

  // The V1 palette lives in CSS custom properties under [data-theme]; Nuxt UI's
  // own dark mode is driven by a `.dark` class. Binding both to one attribute
  // keeps a single source of truth for "which theme is on": `classSuffix: ''`
  // writes `.dark`/`.light` for Nuxt UI and Tailwind, `dataValue` writes
  // `data-theme` for the ported tokens, and `storageKey` reuses V1's localStorage
  // key so an existing user's choice survives the migration.
  colorMode: {
    classSuffix: '',
    dataValue: 'theme',
    preference: 'dark',
    fallback: 'dark',
    storageKey: 'theme',
  },

  ssr: false,

  devServer: { port: 3000 },

  nitro: {
    devProxy: {
      '/api': {
        target: 'http://127.0.0.1:8765/api',
        changeOrigin: true,
      },
    },
  },

  typescript: {
    strict: true,
    typeCheck: false,
  },

  vite: {
    // Vitest resolves this config too; without it the happy-dom environment
    // inherits Nuxt's default `#imports` alias only through @nuxt/test-utils.
    optimizeDeps: { include: ['markdown-it'] },
  },
})
