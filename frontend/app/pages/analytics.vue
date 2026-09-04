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

const { data, status, error } = useAnalytics()

const isLoading = computed(() => status.value === 'pending')
const psr = computed(() => data.value?.kpis.phone_screen_readiness)
</script>

<template>
  <p v-if="isLoading" class="ov-state">
    Loading analytics…
  </p>
  <p v-else-if="error || !data" class="ov-state ov-error">
    Could not load analytics.
  </p>
  <div v-else class="overview">
    <header class="ov-head">
      <h1>Analytics</h1>
      <p class="kpi-sub">
        Last {{ data.days }} days
      </p>
    </header>

    <div class="kpi-grid">
      <KpiTile
        label="Phone-screen readiness"
        :value="asScore(psr?.value)"
        :sub="`target ${psr?.target ?? 90}%`"
      />
      <KpiTile
        label="Response rate"
        :value="asPct(data.kpis.response_rate)"
        sub="replies ÷ applied"
      />
      <KpiTile
        label="Velocity"
        :value="`${data.kpis.velocity.value}`"
        :sub="`goal ${data.kpis.velocity.goal ?? 5} / ${data.kpis.velocity.window_days ?? 7}d`"
      />
    </div>

    <div class="ov-columns">
      <section class="ov-section ov-col">
        <h2 class="ov-section-title">
          Applications per day
        </h2>
        <DayBars :data="data.applications_per_day" />
      </section>
      <section class="ov-section ov-col">
        <h2 class="ov-section-title">
          Recruiter replies per day
        </h2>
        <DayBars :data="data.replies_per_day" accent />
      </section>
    </div>

    <section class="ov-section">
      <h2 class="ov-section-title">
        Phone-screen readiness trend
        <span class="ov-count">target {{ data.phone_screen_trend.target }}%</span>
      </h2>
      <PsTrend :trend="data.phone_screen_trend" />
    </section>

    <div class="ov-columns">
      <section class="ov-section ov-col">
        <h2 class="ov-section-title">
          Funnel
        </h2>
        <FunnelBars :rows="data.funnel" />
      </section>

      <section class="ov-section ov-col">
        <h2 class="ov-section-title">
          Pipeline by status
        </h2>
        <StatusBars :rows="data.status_breakdown" />
      </section>
    </div>
  </div>
</template>
