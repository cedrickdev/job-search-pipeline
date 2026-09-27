<!--
  Career intelligence: the "measure" and "recommend" links of the spine, on one read-only screen.

  The top half is the analytics report — the funnel from submitted to accepted, the four conversion
  rates, the timing medians, and the per-dimension breakdowns (role family, source, …). It is a pure
  read: nothing on this screen writes an application's execution state, and the numbers are computed
  server-side over a maturity horizon, so an application too young to have plausibly resolved is
  counted as censored rather than as a failure (docs/CAREER_INTELLIGENCE.md §measure).

  The bottom half is the recommendations — evidence-backed advice, each citing the metric it rests on
  and carrying nothing it can execute. "Generate" recomputes the report and appends a fresh set to a
  write-once store; it edits no prior suggestion. Acting on one is a separate, human-approved step on
  the strategy surface, which this page only points to — a recommendation has zero authority (§recommend).
-->
<script setup lang="ts">
import { computed } from 'vue'
import {
  useCareerAnalytics,
  useGenerateRecommendations,
  useRecommendationsQuery,
} from '~/composables/useCareer'
import type {
  CareerRecommendation,
  ConversionRate,
  DimensionCell,
  TimingStat,
} from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

definePageMeta({ middleware: 'auth' })

useHead({ title: 'Career intelligence · Command Center' })

const {
  data: analyticsData,
  status: analyticsStatus,
  error: analyticsError,
  refresh: refreshAnalytics,
} = useCareerAnalytics()
const { data: recommendationsData } = useRecommendationsQuery()
const generate = useGenerateRecommendations()

const report = computed(() => analyticsData.value ?? null)
const recommendations = computed<CareerRecommendation[]>(
  () => recommendationsData.value?.recommendations ?? [])

const analyticsLoading = computed(
  () => analyticsStatus.value === 'pending' && !analyticsData.value)

function label(value: string): string {
  return value.replace(/_/g, ' ').toLowerCase()
}

/** A rate as a percent, or a dash when the matured sample was too thin to define one. */
function ratePercent(rate: ConversionRate): string {
  return rate.rate_percent === null ? '—' : `${rate.rate_percent}%`
}

/** A timing's median in days, or a dash when nothing reached that milestone. */
function medianDays(timing: TimingStat): string {
  return timing.median_days === null ? '—' : `${timing.median_days}d`
}

/** A breakdown cell's key, or the honest "unclassified" for the null bucket. */
function cellKey(key: string | null): string {
  return key === null ? 'unclassified' : label(key)
}

/** A cell's leading rate as a trailing clause, or nothing when it cites none. */
function cellRate(cell: DimensionCell): string {
  const first = cell.rates[0]
  return first ? ` · ${label(first.kind)} ${ratePercent(first)}` : ''
}

function onGenerate(): void {
  generate.mutate()
}
</script>

<template>
  <section class="career">
    <header class="career__header">
      <h1>Career intelligence</h1>
      <div class="career__header-actions">
        <UButton
          variant="ghost"
          icon="i-heroicons-arrow-path"
          :loading="analyticsStatus === 'pending'"
          @click="() => refreshAnalytics()"
        >
          Refresh
        </UButton>
        <UButton
          color="primary"
          :loading="generate.isPending"
          data-test="generate"
          @click="onGenerate"
        >
          Generate recommendations
        </UButton>
      </div>
    </header>

    <p v-if="analyticsError" class="career__error" role="alert">
      {{ errorMessage(analyticsError) }}
    </p>
    <p v-else-if="analyticsLoading" class="career__muted">Computing your funnel…</p>
    <p v-else-if="report && report.funnel.window.is_empty" class="career__muted">
      No applications to measure yet. Submit an application and record its outcomes to see your funnel.
    </p>

    <template v-else-if="report">
      <p class="career__muted">
        {{ report.funnel.censoring.mature_count }} of {{ report.funnel.censoring.total_count }}
        applications mature enough to have resolved
        ({{ report.funnel.censoring.observation_horizon_days }}-day horizon);
        {{ report.funnel.censoring.censored_count }} still too recent to count.
      </p>

      <div class="career__block" data-test="funnel">
        <h2>Funnel</h2>
        <ul class="funnel">
          <li v-for="s in report.funnel.stages" :key="s.stage" class="funnel__row">
            <span class="funnel__stage">{{ label(s.stage) }}</span>
            <span
              class="funnel__bar"
              :style="{ width: `${Math.round((s.applications / Math.max(report.funnel.stages[0]?.applications || 1, 1)) * 100)}%` }"
            />
            <span class="funnel__count">{{ s.applications }}</span>
          </li>
        </ul>
      </div>

      <div class="career__block">
        <h2>Conversion rates</h2>
        <div class="rates">
          <div v-for="r in report.rates" :key="r.kind" class="rate">
            <div class="rate__value">{{ ratePercent(r) }}</div>
            <div class="rate__label">{{ label(r.kind) }}</div>
            <div class="rate__n">{{ r.numerator }}/{{ r.denominator }} · n={{ r.sample_size }}</div>
          </div>
        </div>
      </div>

      <div class="career__block">
        <h2>Timing</h2>
        <ul class="timings">
          <li v-for="t in report.timings" :key="t.kind">
            <span>{{ label(t.kind) }}</span>
            <span>{{ medianDays(t) }} <span class="rate__n">(n={{ t.sample_size }})</span></span>
          </li>
        </ul>
      </div>

      <div v-if="report.breakdowns.length > 0" class="career__block">
        <h2>Breakdowns</h2>
        <div v-for="b in report.breakdowns" :key="b.dimension" class="breakdown">
          <h3>{{ label(b.dimension) }}</h3>
          <div v-for="c in b.cells" :key="c.key ?? 'none'" class="breakdown__cell">
            <span class="breakdown__key">{{ cellKey(c.key) }}</span>
            <span class="breakdown__stat">
              {{ c.applications }} app(s){{ cellRate(c) }}
            </span>
          </div>
        </div>
      </div>
    </template>

    <div class="career__block" data-test="recommendations">
      <h2>Recommendations</h2>
      <p v-if="generate.error" class="career__error" role="alert">
        {{ errorMessage(generate.error) }}
      </p>
      <p v-if="recommendations.length === 0" class="career__muted">
        No recommendations yet. Generate a set to see evidence-backed suggestions drawn from your funnel.
      </p>
      <ul v-else class="recs">
        <RecommendationCard
          v-for="rec in recommendations"
          :key="rec.id"
          :recommendation="rec"
        />
      </ul>
      <p class="career__pointer">
        A recommendation is advice, never a change. Acting on one is a reviewed step on the
        <NuxtLink to="/strategy">strategy</NuxtLink> surface.
      </p>
    </div>
  </section>
</template>

<style scoped>
.career { display: flex; flex-direction: column; gap: 1.25rem; }
.career__header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.career__header h1 { font-size: 1.25rem; font-weight: 600; margin: 0; }
.career__header-actions { display: flex; align-items: center; gap: 0.5rem; }
.career__error { color: var(--ui-error, #dc2626); margin: 0; }
.career__muted { color: var(--ui-text-muted, #6b7280); font-size: 0.85rem; margin: 0; }
.career__block { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.85rem 1rem; display: flex; flex-direction: column; gap: 0.6rem; }
.career__block h2 { font-size: 0.95rem; font-weight: 600; margin: 0; }
.funnel { display: flex; flex-direction: column; gap: 0.3rem; list-style: none; margin: 0; padding: 0; }
.funnel__row { display: flex; align-items: center; gap: 0.6rem; font-size: 0.85rem; }
.funnel__stage { min-width: 8rem; text-transform: capitalize; }
.funnel__bar { height: 0.6rem; border-radius: 999px; background: var(--ui-primary, #6366f1); min-width: 2px; }
.funnel__count { font-variant-numeric: tabular-nums; font-weight: 600; }
.rates { display: flex; flex-wrap: wrap; gap: 0.6rem; }
.rate { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.4rem; padding: 0.4rem 0.7rem; min-width: 9rem; }
.rate__value { font-size: 1.2rem; font-weight: 700; }
.rate__label { font-size: 0.72rem; color: var(--ui-text-muted, #6b7280); text-transform: capitalize; }
.rate__n { font-size: 0.7rem; color: var(--ui-text-muted, #9ca3af); }
.timings { display: flex; flex-direction: column; gap: 0.2rem; list-style: none; margin: 0; padding: 0; font-size: 0.85rem; }
.timings li { display: flex; justify-content: space-between; }
.breakdown { display: flex; flex-direction: column; gap: 0.3rem; }
.breakdown h3 { font-size: 0.8rem; font-weight: 600; margin: 0; text-transform: capitalize; color: var(--ui-text-muted, #4b5563); }
.breakdown__cell { display: flex; justify-content: space-between; gap: 0.5rem; font-size: 0.8rem; }
.breakdown__key { text-transform: capitalize; }
.breakdown__stat { color: var(--ui-text-muted, #6b7280); font-variant-numeric: tabular-nums; }
.recs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
.career__pointer { font-size: 0.85rem; color: var(--ui-text-muted, #6b7280); }
</style>
