<script setup lang="ts">
import { useLocale } from '~/composables/useLocale'

definePageMeta({ layout: false })
const { locale, localize: l } = useLocale()
useHead({ title: () => `${l('Terms of Service', 'Conditions générales')} · Command Center` })
</script>

<template>
  <main class="auth-page">
    <article class="auth-card auth-card--wide legal-page">
      <h1>{{ locale === 'fr' ? 'Conditions générales' : 'Terms of Service' }}</h1>
      <p class="auth-hint">
        {{ locale === 'fr'
          ? 'Cette application locale prépare des candidatures à partir des informations que vous fournissez. Vous restez responsable de la vérification et de l’envoi final.'
          : 'This local application prepares applications from information you provide. You remain responsible for reviewing and submitting the final application.' }}
      </p>
      <h2>{{ locale === 'fr' ? 'Données et sécurité' : 'Data and security' }}</h2>
      <p>
        {{ locale === 'fr'
          ? 'Les secrets sont conservés côté serveur et ne sont jamais affichés dans l’interface. Les CAPTCHA, MFA et formulaires ambigus nécessitent une intervention humaine.'
          : 'Secrets are stored server-side and never displayed in the interface. CAPTCHAs, MFA and ambiguous forms require human intervention.' }}
      </p>
      <h2>{{ locale === 'fr' ? 'Utilisation responsable' : 'Responsible use' }}</h2>
      <p>
        {{ locale === 'fr'
          ? 'Vous autorisez uniquement les candidatures conformes à vos critères et à votre limite quotidienne. Aucun test automatisé ne doit envoyer une candidature réelle.'
          : 'You authorize only applications matching your criteria and daily limit. Automated tests must never send a real application.' }}
      </p>
      <NuxtLink class="btn-primary" to="/onboarding">
        {{ locale === 'fr' ? 'Retour à la configuration' : 'Back to setup' }}
      </NuxtLink>
    </article>
  </main>
</template>
