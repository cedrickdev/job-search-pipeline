<!--
  Billing: the commercial surface a browser reads, and the two hosted opens it starts.

  The page shows three server-authoritative reads — the plan catalogue, this account's current
  plan and subscription, and its usage against that plan — and nothing on it computes a price, a
  quota or an entitlement. Those are server truth; the client renders what the catalogue serves
  (§17, §61), which is why there is no field here a request could set a price or a limit through.

  It writes nothing to a subscription. "Subscribe" opens a hosted checkout and "Manage billing"
  opens the hosted portal; each returns a `redirect_url` the browser is sent to. A browser never
  moves a subscription — the provider drives the change and a verified webhook applies it (§16) —
  so the overview here refreshes on the next read, not optimistically off a click. The checkout
  body names only a plan slug; the success and cancel URLs are the server's (§18).
-->
<script setup lang="ts">
import { computed } from 'vue'
import {
  useOpenCheckout,
  useOpenPortal,
  usePlansQuery,
  useSubscriptionQuery,
  useUsageQuery,
} from '~/composables/useBilling'
import type { Plan, UsageLine } from '~/types/v2'
import { errorMessage } from '~/utils/v2-errors'

definePageMeta({ middleware: 'auth' })

useHead({ title: 'Billing · Command Center' })

const { data: plansData, status: plansStatus, error: plansError } = usePlansQuery()
const { data: subscriptionData, error: subscriptionError } = useSubscriptionQuery()
const { data: usageData } = useUsageQuery()
const checkout = useOpenCheckout()
const portal = useOpenPortal()

const plans = computed<Plan[]>(() => plansData.value?.plans ?? [])
const overview = computed(() => subscriptionData.value ?? null)
const usageLines = computed<UsageLine[]>(() => usageData.value?.lines ?? [])
const currentSlug = computed(() => overview.value?.plan.slug ?? null)

const plansLoading = computed(() => plansStatus.value === 'pending' && !plansData.value)

function label(value: string): string {
  return value.replace(/_/g, ' ').toLowerCase()
}

/** A plan's price as a short line, or "Free" for the free tier. */
function priceLabel(plan: Plan): string {
  if (plan.is_free || plan.price_amount_cents === null) return 'Free'
  const amount = (plan.price_amount_cents / 100).toFixed(2)
  const currency = plan.currency ? `${plan.currency} ` : ''
  const interval = plan.billing_interval ? ` / ${label(plan.billing_interval)}` : ''
  return `${currency}${amount}${interval}`
}

/** One entitlement's ceiling as a word — "unlimited", or the number the plan grants. */
function limitLabel(limit: number | null, isUnlimited: boolean): string {
  return isUnlimited || limit === null ? 'unlimited' : String(limit)
}

/** A usage line's remaining as a clause, tolerating the unlimited (null-limit) case. */
function remainingLabel(line: UsageLine): string {
  return line.limit === null ? 'unlimited' : `${line.remaining ?? 0} left`
}

/** The width of a usage bar as a percent, or 0 while the line is unbounded. */
function usedPercent(line: UsageLine): number {
  if (line.limit === null || line.limit === 0) return 0
  return Math.min(100, Math.round((line.used / line.limit) * 100))
}

function onCheckout(slug: string): void {
  checkout
    .mutateAsync(slug)
    .then((res) => { void navigateTo(res.redirect_url, { external: true }) })
    .catch(() => {})
}

function onPortal(): void {
  portal
    .mutateAsync()
    .then((res) => { void navigateTo(res.redirect_url, { external: true }) })
    .catch(() => {})
}
</script>

<template>
  <section class="billing">
    <header class="billing__header">
      <h1>Billing</h1>
      <UButton
        v-if="overview?.can_manage_billing"
        variant="ghost"
        icon="i-heroicons-credit-card"
        :loading="portal.isPending"
        data-test="portal"
        @click="onPortal"
      >
        Manage billing
      </UButton>
    </header>

    <p v-if="checkout.error" class="billing__error" role="alert">
      {{ errorMessage(checkout.error) }}
    </p>
    <p v-if="portal.error" class="billing__error" role="alert">
      {{ errorMessage(portal.error) }}
    </p>

    <div v-if="overview" class="billing__block" data-test="current">
      <h2>Your plan</h2>
      <div class="current">
        <div>
          <div class="current__name">{{ overview.plan.name }}</div>
          <div class="billing__muted">
            {{ overview.is_paid ? 'Paid' : 'Free' }}
            <template v-if="overview.status"> · {{ label(overview.status) }}</template>
            <template v-if="overview.cancel_at_period_end"> · cancels at period end</template>
          </div>
        </div>
        <div class="current__period billing__muted">
          Billing period {{ overview.period.label }}
        </div>
      </div>
    </div>
    <p v-else-if="subscriptionError" class="billing__error" role="alert">
      {{ errorMessage(subscriptionError) }}
    </p>

    <div class="billing__block" data-test="usage">
      <h2>Usage</h2>
      <p v-if="usageLines.length === 0" class="billing__muted">No usage to show yet.</p>
      <ul v-else class="usage">
        <li v-for="line in usageLines" :key="line.key" class="usage__row">
          <span class="usage__key">{{ label(line.key) }}</span>
          <span
            class="usage__bar"
            :style="{ width: `${usedPercent(line)}%` }"
            :class="{ 'usage__bar--empty': line.limit === null }"
          />
          <span class="usage__stat">
            {{ line.used }} / {{ limitLabel(line.limit, false) }} · {{ remainingLabel(line) }}
          </span>
        </li>
      </ul>
    </div>

    <div class="billing__block" data-test="plans">
      <h2>Plans</h2>
      <p v-if="plansError" class="billing__error" role="alert">
        {{ errorMessage(plansError) }}
      </p>
      <p v-else-if="plansLoading" class="billing__muted">Loading plans…</p>
      <div v-else class="plans">
        <article
          v-for="plan in plans"
          :key="plan.slug"
          class="plan"
          :class="{ 'plan--current': plan.slug === currentSlug }"
          :data-test="`plan-${plan.slug}`"
        >
          <header class="plan__head">
            <span class="plan__name">{{ plan.name }}</span>
            <span class="plan__price">{{ priceLabel(plan) }}</span>
          </header>
          <p v-if="plan.description" class="billing__muted">{{ plan.description }}</p>
          <ul class="plan__entitlements">
            <li v-for="e in plan.entitlements" :key="e.key">
              <span>{{ label(e.key) }}</span>
              <span class="billing__muted">{{ limitLabel(e.limit, e.is_unlimited) }}</span>
            </li>
          </ul>
          <div class="plan__action">
            <span v-if="plan.slug === currentSlug" class="plan__current">Current plan</span>
            <UButton
              v-else-if="!plan.is_free"
              color="primary"
              size="sm"
              :loading="checkout.isPending"
              :data-test="`subscribe-${plan.slug}`"
              @click="onCheckout(plan.slug)"
            >
              Subscribe
            </UButton>
          </div>
        </article>
      </div>
    </div>
  </section>
</template>

<style scoped>
.billing { display: flex; flex-direction: column; gap: 1.25rem; }
.billing__header { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.billing__header h1 { font-size: 1.25rem; font-weight: 600; margin: 0; }
.billing__error { color: var(--ui-error, #dc2626); margin: 0; }
.billing__muted { color: var(--ui-text-muted, #6b7280); font-size: 0.85rem; margin: 0; }
.billing__block { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.85rem 1rem; display: flex; flex-direction: column; gap: 0.6rem; }
.billing__block h2 { font-size: 0.95rem; font-weight: 600; margin: 0; }
.current { display: flex; align-items: center; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.current__name { font-size: 1.05rem; font-weight: 600; }
.usage { display: flex; flex-direction: column; gap: 0.4rem; list-style: none; margin: 0; padding: 0; }
.usage__row { display: flex; align-items: center; gap: 0.6rem; font-size: 0.85rem; }
.usage__key { min-width: 12rem; text-transform: capitalize; }
.usage__bar { height: 0.6rem; border-radius: 999px; background: var(--ui-primary, #6366f1); min-width: 2px; max-width: 8rem; }
.usage__bar--empty { background: var(--ui-border, #e5e7eb); }
.usage__stat { color: var(--ui-text-muted, #6b7280); font-variant-numeric: tabular-nums; }
.plans { display: flex; flex-wrap: wrap; gap: 0.75rem; }
.plan { border: 1px solid var(--ui-border, #e5e7eb); border-radius: 0.5rem; padding: 0.75rem 0.9rem; display: flex; flex-direction: column; gap: 0.5rem; min-width: 14rem; flex: 1 1 14rem; }
.plan--current { border-color: var(--ui-primary, #6366f1); }
.plan__head { display: flex; align-items: baseline; justify-content: space-between; gap: 0.5rem; }
.plan__name { font-weight: 600; text-transform: capitalize; }
.plan__price { font-variant-numeric: tabular-nums; }
.plan__entitlements { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.8rem; }
.plan__entitlements li { display: flex; justify-content: space-between; gap: 0.5rem; text-transform: capitalize; }
.plan__action { margin-top: auto; }
.plan__current { font-size: 0.8rem; font-weight: 600; color: var(--ui-primary, #6366f1); }
</style>
