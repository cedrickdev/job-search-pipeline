// The `/api/v2/billing` surface: the commercial spine, read-only but for the two hosted opens.
//
// Three rules of Phase 16 show up here as plumbing decisions.
//
// **The client defines no price, quota or entitlement — it reads them.** The catalogue, the
// subscription overview and the usage snapshot are all pure reads of server truth (§17, §61);
// nothing in this file computes a limit or a price. A quota is a server decision, and the numbers
// a `/billing` panel shows are the numbers a quota check reads.
//
// **A subscription changes only when the provider says so.** There is no "subscribe" mutation
// that writes a plan — a browser never moves a subscription (§16). What the two mutations do is
// open a *hosted* session: checkout to buy, portal to manage, each answered with a `redirect_url`
// the caller sends the browser to. The provider then drives the change and a verified webhook
// applies it server-side, which is why neither mutation invalidates the overview optimistically:
// the truth arrives out of band, on the next read.
//
// **The URLs are the server's, never the request's.** A checkout body names only a plan slug; the
// success and cancel URLs are built from `SiteSettings` server-side, so there is no field here to
// smuggle a redirect through (§18).
import { apiGet, apiPost } from '~/utils/api-client'
import type {
  CheckoutResponse,
  PlanList,
  PortalResponse,
  SubscriptionOverview,
  UsageSnapshot,
} from '~/types/v2'
import { V2_ENDPOINTS } from '~/utils/endpoints'
import { useApiQuery } from './useApiQuery'
import { useMutation } from './useMutation'

/** The public catalogue, cheapest first — a read every visitor to the page needs. */
export function usePlansQuery() {
  return useApiQuery<PlanList>('billing:plans', () =>
    apiGet<PlanList>(V2_ENDPOINTS.billingPlans))
}

/** This account's plan and live subscription — the free tier when it has never subscribed. */
export function useSubscriptionQuery() {
  return useApiQuery<SubscriptionOverview>('billing:subscription', () =>
    apiGet<SubscriptionOverview>(V2_ENDPOINTS.billingSubscription))
}

/** This account's usage against its plan, one line per metered capability. */
export function useUsageQuery() {
  return useApiQuery<UsageSnapshot>('billing:usage', () =>
    apiGet<UsageSnapshot>(V2_ENDPOINTS.billingUsage))
}

/**
 * Open a hosted checkout for a plan slug, resolving to the provider redirect URL.
 *
 * No cache invalidation: buying does not change the subscription here — the provider drives the
 * purchase and a verified webhook applies it (§16). The caller sends the browser to `redirect_url`.
 * The body carries only the slug; the success and cancel URLs are the server's (§18).
 */
export function useOpenCheckout() {
  return useMutation<string, CheckoutResponse>(planSlug =>
    apiPost<CheckoutResponse>(V2_ENDPOINTS.billingCheckout, { plan_slug: planSlug }))
}

/**
 * Open the hosted billing portal for this account's provider customer, resolving to its redirect URL.
 *
 * Refuses with `billing_customer_missing` (a 409) when the account never subscribed and so has no
 * provider customer to manage. Like checkout, it changes nothing locally — the portal is where a
 * downgrade or cancellation happens, and the resulting webhook is what moves the subscription.
 */
export function useOpenPortal() {
  return useMutation<void, PortalResponse>(() =>
    apiPost<PortalResponse>(V2_ENDPOINTS.billingPortal))
}
