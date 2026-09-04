<!--
  Overview — the daily working view. Ported from webapp/src/routes/OverviewPage.tsx
  (React route "/").

  Every row here opens /jobs rather than a drawer, exactly as V1 did: the drawer
  lives on the jobs page, so the overview navigates and lets that page own
  selection. Keeping it that way avoids a second drawer mount point with its own
  polling.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useOverview } from '~/composables/useQueries'
import { asPct, asScore } from '~/utils/format'

const { data, status, error } = useOverview()

const isLoading = computed(() => status.value === 'pending')
const psr = computed(() => data.value?.kpis.phone_screen_readiness)

/** Every list on this page hands selection to the jobs page. */
function openJobs() {
  return navigateTo('/jobs')
}

/** Source health rows are open-ended dicts: show every key except the name. */
function stats(source: Record<string, unknown>): [string, unknown][] {
  return Object.entries(source).filter(([k]) => k !== 'source')
}
</script>

<template>
  <p v-if="isLoading" class="ov-state">
    Loading overview…
  </p>
  <p v-else-if="error || !data" class="ov-state ov-error">
    Could not load overview.
  </p>
  <div v-else class="overview">
    <header class="ov-head">
      <h1>Overview</h1>
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
        :sub="`goal ${data.kpis.velocity.goal ?? 5} / 7d`"
      />
      <KpiTile label="In flight" :value="`${data.kpis.in_flight}`" sub="active applications" />
      <KpiTile
        label="Replies to action"
        :value="`${data.kpis.replies_to_action}`"
        sub="need a response"
      />
    </div>

    <div class="ov-columns">
      <div class="ov-col">
        <JobSection title="Today — do these first" :jobs="data.today" @open="openJobs" />
        <JobSection title="Borderline review" :jobs="data.borderline" @open="openJobs" />
        <JobSection title="Auto-approved today" :jobs="data.auto_approved_today" @open="openJobs" />
      </div>

      <div class="ov-col">
        <JobSection title="Recruiter replies" :jobs="data.replies_to_action" @open="openJobs" />

        <section class="ov-section">
          <h2 class="ov-section-title">
            Upcoming interviews
            <span class="ov-count">{{ data.upcoming_interviews.length }}</span>
          </h2>
          <p v-if="data.upcoming_interviews.length === 0" class="ov-empty">
            No interviews scheduled.
          </p>
          <div v-else class="job-list">
            <button
              v-for="iv in data.upcoming_interviews"
              :key="iv.id"
              type="button"
              class="job-row"
              @click="openJobs"
            >
              <span class="job-row-main">
                <span class="job-row-company">{{ iv.company }}</span>
                <span class="job-row-title">{{ iv.round_label }} · {{ iv.title }}</span>
              </span>
              <span class="job-row-meta">
                <span class="ov-when">{{ iv.scheduled_for ?? 'TBD' }}</span>
              </span>
            </button>
          </div>
        </section>

        <FollowupsPanel :items="data.followups_due" @open="openJobs" />
      </div>
    </div>

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

    <section class="ov-section">
      <h2 class="ov-section-title">
        Source health
      </h2>
      <p v-if="data.source_health.length === 0" class="ov-empty">
        No source data yet.
      </p>
      <div v-else class="ov-sources">
        <div v-for="s in data.source_health" :key="s.source" class="ov-source">
          <span class="ov-source-name">{{ s.source }}</span>
          <span class="ov-source-stats">
            <span v-for="[k, v] in stats(s)" :key="k" class="ov-source-stat">
              {{ k.replace(/_/g, ' ') }}: {{ String(v) }}
            </span>
          </span>
        </div>
      </div>
    </section>
  </div>
</template>
