<!--
  Analytics — trends over a window. Ported from
  webapp/src/routes/AnalyticsPage.tsx (React route "/analytics").

  The window is the backend's default (30 days); V1 never exposed a picker, so
  neither does this.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useAnalytics } from '~/composables/useQueries'
import { asPct, asScore } from '~/utils/format'

const { t, locale, localize: l } = useLocale()
const { data, status, error } = useAnalytics()

const isLoading = computed(() => status.value === 'pending')
const psr = computed(() => data.value?.kpis.phone_screen_readiness)
const funnel = computed(() => (data.value?.funnel ?? []).map(row => ({
  ...row,
  label: locale.value === 'fr'
    ? ({ Discovered: 'Découvertes', Applied: 'Candidatures envoyées' } as Record<string, string>)[row.stage] ?? row.stage
    : row.stage,
})))
const statuses = computed(() => (data.value?.status_breakdown ?? []).map(row => ({
  ...row,
  label: locale.value === 'fr'
    ? ({ Applied: 'Envoyées', 'Recruiter reply': 'Réponse recruteur' } as Record<string, string>)[row.status] ?? row.status
    : row.status,
})))
</script>

<template>
  <p v-if="isLoading" class="ov-state">
    {{ l('Loading analytics…', 'Chargement des analyses…') }}
  </p>
  <p v-else-if="error || !data" class="ov-state ov-error">
    {{ l('Could not load analytics.', 'Impossible de charger les analyses.') }}
  </p>
  <div v-else class="overview">
    <header class="ov-head">
      <h1>{{ t('page.analytics') }}</h1>
      <p class="kpi-sub">
        {{ l(`Last ${data.days} days`, `${data.days} derniers jours`) }}
      </p>
    </header>

    <div class="kpi-grid">
      <KpiTile
        :label="l('Phone-screen readiness', 'Préparation à la présélection')"
        :value="asScore(psr?.value)"
        :sub="l(`target ${psr?.target ?? 90}%`, `objectif ${psr?.target ?? 90}%`)"
      />
      <KpiTile
        :label="l('Response rate', 'Taux de réponse')"
        :value="asPct(data.kpis.response_rate)"
        :sub="l('replies ÷ applied', 'réponses ÷ candidatures')"
      />
      <KpiTile
        :label="l('Velocity', 'Rythme')"
        :value="`${data.kpis.velocity.value}`"
        :sub="l(`goal ${data.kpis.velocity.goal ?? 5} / ${data.kpis.velocity.window_days ?? 7}d`, `objectif ${data.kpis.velocity.goal ?? 5} / ${data.kpis.velocity.window_days ?? 7} j` )"
      />
    </div>

    <div class="ov-columns">
      <section class="ov-section ov-col">
        <h2 class="ov-section-title">
          {{ l('Applications per day', 'Candidatures par jour') }}
        </h2>
        <DayBars :data="data.applications_per_day" />
      </section>
      <section class="ov-section ov-col">
        <h2 class="ov-section-title">
          {{ l('Recruiter replies per day', 'Réponses des recruteurs par jour') }}
        </h2>
        <DayBars :data="data.replies_per_day" accent />
      </section>
    </div>

    <section class="ov-section">
      <h2 class="ov-section-title">
        {{ l('Phone-screen readiness trend', 'Évolution de la préparation à la présélection') }}
        <span class="ov-count">{{ l('target', 'objectif') }} {{ data.phone_screen_trend.target }}%</span>
      </h2>
      <PsTrend :trend="data.phone_screen_trend" />
    </section>

    <div class="ov-columns">
      <section class="ov-section ov-col">
        <h2 class="ov-section-title">
          {{ l('Funnel', 'Parcours') }}
        </h2>
        <FunnelBars :rows="funnel" />
      </section>

      <section class="ov-section ov-col">
        <h2 class="ov-section-title">
          {{ l('Pipeline by status', 'Candidatures par statut') }}
        </h2>
        <StatusBars :rows="statuses" />
      </section>
    </div>
  </div>
</template>
