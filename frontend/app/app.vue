<!--
  Root component. `UApp` is Nuxt UI's provider — it supplies the overlay/toast
  portals its components mount into — and it wraps the layout so every page is
  inside it.

  The head block reproduces webapp/index.html's title and inline favicon. The two
  Google Fonts <link>s it also had are gone: `tokens.css` names Sora and IBM Plex
  Mono in `--font-ui`/`--font-mono`, and @nuxt/fonts (pulled in by Nuxt UI)
  resolves those families at build time and self-hosts the woff2 files. Same
  faces, no request to fonts.googleapis.com — which is the behaviour the Settings
  page already promises ("no data leaves your computer").

  `data-theme` is not set here — @nuxtjs/color-mode writes it on `<html>` before
  paint from its own inline script, which is what avoids the flash V1's hardcoded
  `data-theme="dark"` attribute avoided.
-->
<script setup lang="ts">
const FAVICON
  = 'data:image/svg+xml,%3Csvg%20xmlns=\'http://www.w3.org/2000/svg\'%20viewBox=\'0%200%20100%20100\'%3E%3Ctext%20y=\'.9em\'%20font-size=\'90\'%3E%E2%8C%98%3C/text%3E%3C/svg%3E'

useHead({
  title: 'Job Search Command Center',
  htmlAttrs: { lang: 'en' },
  link: [{ rel: 'icon', href: FAVICON }],
})
</script>

<template>
  <UApp>
    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </UApp>
</template>
