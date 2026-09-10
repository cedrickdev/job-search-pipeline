<!--
  One employer, and where every claim on the page came from.

  The detail view exists to make provenance visible, because Phase 6's facts are
  inferences: "on Greenhouse" was concluded from a URL or from configuration, and the
  two are not equally strong. So nothing here is shown bare — the ATS badge carries
  its `status` and its evidence, the spontaneous-application verdict carries whoever
  observed it, and the discovery records name the provider and the moment.

  What is deliberately absent: the `raw` provider metadata. The API has no field for
  it (`CompanyDiscoveryRecordResponse`), so this page could not show it if it tried,
  and that is the intended arrangement rather than a gap to fill later (§29).

  `UNKNOWN` is rendered as itself, never as "no". A verdict nobody has formed and a
  verdict of refusal are different facts, and only one of them is a reason not to
  apply.
-->
<script setup lang="ts">
import { computed } from 'vue'
import { useCompanyQuery } from '~/composables/useCompanies'
import type { CompanyDetail } from '~/types/v2'

definePageMeta({ middleware: 'auth' })

const route = useRoute()
const companyId = computed(() => {
  const raw = route.params.id
  const value = Array.isArray(raw) ? raw[0] : raw
  return value ? value : null
})

const { data, status, error } = useCompanyQuery(companyId)

const company = computed(() => data.value?.company ?? null)
const isLoading = computed(() => status.value === 'pending' && data.value === undefined)
// `null`, not an error: the query maps `company_not_found` to it, because an id out of
// a stale link is a screen to render rather than a failure to report.
const isMissing = computed(() => !error.value && data.value === null)

useHead({
  title: () => `${company.value?.name ?? 'Company'} · Command Center`,
})

/** The spontaneous-application verdict in words, `UNKNOWN` kept as itself. */
const spontaneous = computed(() => {
  const channel = company.value?.spontaneous_application
  if (!channel) return { label: 'Unknown — nobody has checked', url: null as string | null }
  const label = channel.support === 'SUPPORTED'
    ? 'Supported'
    : channel.support === 'NOT_SUPPORTED'
      ? 'Not supported'
      : 'Unknown — nobody has checked'
  return { label, url: channel.url }
})

/** One location as a line of text. Phase 6 has no coordinates to plot. */
function place(entry: CompanyDetail['company']['locations'][number]): string {
  const parts = [entry.city, entry.region, entry.postal_code, entry.country]
    .filter(part => part !== null && part !== undefined && part !== '')
  return parts.length ? parts.join(', ') : (entry.raw ?? '—')
}

function moment(value: string | null): string {
  if (!value) return 'never'
  const at = new Date(value)
  return Number.isNaN(at.getTime()) ? value : at.toLocaleString()
}
</script>

<template>
  <div class="acct-page co-page">
    <p v-if="isLoading" class="ov-state">
      Loading company…
    </p>
    <p v-else-if="isMissing" class="ov-state">
      That company is not in the directory.
      <NuxtLink to="/companies">
        Back to companies
      </NuxtLink>
    </p>
    <p v-else-if="error || !data || !company" class="ov-state ov-error">
      Could not load this company.
    </p>
    <template v-else>
      <header class="acct-head">
        <h1>{{ company.name }}</h1>
        <NuxtLink to="/companies" class="btn-ghost">
          ← Companies
        </NuxtLink>
      </header>

      <section class="co-section">
        <h2>Facts</h2>
        <dl class="co-facts">
          <dt>Identity</dt>
          <dd>{{ company.identity_status }}</dd>
          <dt>Normalized name</dt>
          <dd><code>{{ company.normalized_name }}</code></dd>
          <dt>Country</dt>
          <dd>{{ company.country ?? '—' }}</dd>
          <dt>Website</dt>
          <dd>
            <a v-if="company.website" :href="company.website" rel="noreferrer noopener" target="_blank">
              {{ company.website }}
            </a>
            <span v-else>—</span>
          </dd>
          <dt>Careers</dt>
          <dd>
            <a v-if="company.careers_url" :href="company.careers_url" rel="noreferrer noopener" target="_blank">
              {{ company.careers_url }}
            </a>
            <span v-else>—</span>
          </dd>
          <dt>Spontaneous applications</dt>
          <dd>
            {{ spontaneous.label }}
            <a v-if="spontaneous.url" :href="spontaneous.url" rel="noreferrer noopener" target="_blank">
              (form)
            </a>
          </dd>
          <dt>Discovered by</dt>
          <dd>{{ data.discovered_by.join(', ') || '—' }}</dd>
        </dl>
      </section>

      <section v-if="company.detected_ats" class="co-section">
        <h2>Applicant tracking system</h2>
        <p class="acct-search-row">
          <span class="ov-count">{{ company.detected_ats.platform }}</span>
          <span class="ov-count">{{ company.detected_ats.status }}</span>
          <span class="acct-search-meta">
            org {{ company.detected_ats.organization_id ?? 'unknown' }} ·
            detected by {{ company.detected_ats.detected_by }}
          </span>
        </p>
        <ul class="co-evidence">
          <li v-for="item in company.detected_ats.evidence" :key="item.code">
            <code>{{ item.code }}</code> — {{ item.detail }}
            <a v-if="item.source_url" :href="item.source_url" rel="noreferrer noopener" target="_blank">
              source
            </a>
          </li>
        </ul>
      </section>

      <section v-if="company.spontaneous_application?.evidence.length" class="co-section">
        <h2>Spontaneous-application evidence</h2>
        <p class="settings-hint">
          Observed by {{ company.spontaneous_application.observed_by ?? 'an unnamed provider' }}.
        </p>
        <ul class="co-evidence">
          <li v-for="item in company.spontaneous_application.evidence" :key="item.code">
            <code>{{ item.code }}</code> — {{ item.detail }}
          </li>
        </ul>
      </section>

      <section v-if="company.locations.length" class="co-section">
        <h2>Locations</h2>
        <ul class="acct-searches">
          <li v-for="(entry, index) in company.locations" :key="index" class="acct-search">
            <div class="acct-search-row">
              <span class="acct-search-main">{{ place(entry) }}</span>
              <span v-if="entry.is_headquarters" class="ov-count">HQ</span>
            </div>
          </li>
        </ul>
      </section>

      <section v-if="data.career_sites.length" class="co-section">
        <h2>Career pages</h2>
        <ul class="acct-searches">
          <li v-for="site in data.career_sites" :key="site.url" class="acct-search">
            <div class="acct-search-row">
              <a :href="site.url" class="acct-search-name" rel="noreferrer noopener" target="_blank">
                {{ site.url }}
              </a>
              <span class="ov-count">{{ site.kind }}</span>
              <span v-if="site.platform" class="ov-count">{{ site.platform }}</span>
              <span class="ov-count">{{ site.verification_status }}</span>
            </div>
            <span class="acct-search-meta">
              from {{ site.source_key }} · found {{ moment(site.discovered_at) }} ·
              checked {{ moment(site.last_checked_at) }}
            </span>
          </li>
        </ul>
      </section>

      <section v-if="data.aliases.length" class="co-section">
        <h2>Also known as</h2>
        <ul class="acct-searches">
          <li v-for="alias in data.aliases" :key="`${alias.source_key}:${alias.normalized_alias}`" class="acct-search">
            <div class="acct-search-row">
              <span class="acct-search-main acct-search-name">{{ alias.alias }}</span>
              <span class="acct-search-meta">
                from {{ alias.source_key }} · first {{ moment(alias.first_seen_at) }} ·
                last {{ moment(alias.last_seen_at) }}
              </span>
            </div>
          </li>
        </ul>
      </section>

      <section v-if="data.discoveries.length" class="co-section">
        <h2>Provenance</h2>
        <ul class="acct-searches">
          <li
            v-for="record in data.discoveries"
            :key="`${record.provider_key}:${record.external_id}`"
            class="acct-search"
          >
            <div class="acct-search-row">
              <span class="acct-search-main acct-search-name">{{ record.company_name }}</span>
              <span class="ov-count">{{ record.seed_kind }}</span>
              <span class="ov-count">{{ record.confidence }}</span>
            </div>
            <span class="acct-search-meta">
              {{ record.provider_key }} · <code>{{ record.external_id }}</code> ·
              {{ moment(record.discovered_at) }}
            </span>
            <a v-if="record.source_url" :href="record.source_url" rel="noreferrer noopener" target="_blank">
              {{ record.source_url }}
            </a>
          </li>
        </ul>
      </section>
    </template>
  </div>
</template>
