// The `/api/v2/career/strategy-proposals` surface: the spine's only mutation, gated behind a
// human. A proposal is a typed, bounded description of an edit to one search or one application
// policy; it changes nothing until the user confirms it, and approval hands the confirmed change
// to the same service that already owns that edit (docs/CAREER_INTELLIGENCE.md §The approval gate).
//
// The one rule the whole loop exists to protect lives on the server and surfaces here as a status
// a caller must handle: a change that loosens a safety brake — lowering the score floor, raising a
// cap, widening what the platform may apply to — is refused with `sensitive_confirmation_required`
// unless `confirm_sensitive` is set. This composable does not swallow that refusal; the page reads
// `error.code` and asks for the deliberate second yes. "The system never silently expands the
// user's application policy" is that round-trip.
//
// Two queues, because they answer different questions: the pending list is the review queue
// (still-open proposals), history is every status (what was done). Both are invalidated by an
// approve or a dismiss, since either moves a proposal out of the open set and into the record.
import type {
  StrategyChangeExecution,
  StrategyChangeProposal,
  StrategyChangeProposalList,
} from '~/types/v2'
import { apiGet, apiPost } from '~/utils/api-client'
import { V2_ENDPOINTS, strategyProposal } from '~/utils/endpoints'
import { invalidate, useApiQuery } from './useApiQuery'
import { useMutation } from './useMutation'

/** The two prefixes an approve or dismiss refreshes. */
const PENDING_KEY = 'strategy-proposals'
const HISTORY_KEY = 'strategy-proposals-history'

/** This account's still-open proposals, most recently updated first — the review queue. */
export function usePendingProposalsQuery() {
  return useApiQuery<StrategyChangeProposalList>(PENDING_KEY, () =>
    apiGet<StrategyChangeProposalList>(V2_ENDPOINTS.strategyProposals))
}

/** This account's proposals in every status — a surface to review what was done. */
export function useProposalHistoryQuery() {
  return useApiQuery<StrategyChangeProposalList>(HISTORY_KEY, () =>
    apiGet<StrategyChangeProposalList>(V2_ENDPOINTS.strategyProposalsHistory))
}

/** The argument to approve: which proposal, and whether the sensitive second gate is cleared. */
export interface ApproveArgs {
  proposalId: string
  confirmSensitive?: boolean
}

/**
 * The two verbs the review queue offers, grouped so a call site holds them with separate pending
 * flags — an approve in flight on one card must not disable the dismiss on another.
 *
 * Approve carries `confirm_sensitive`: left false (the default), a loosening change is refused with
 * `sensitive_confirmation_required` and the proposal stays open; set true, the deliberate second
 * acknowledgement lets it through. A non-sensitive proposal ignores the flag. Both verbs refresh
 * the pending queue and the history, so a moved proposal disappears from one and appears in the
 * other together.
 */
export function useStrategyProposalActions() {
  const refresh = () => invalidate(PENDING_KEY, HISTORY_KEY)
  return {
    approve: useMutation<ApproveArgs, StrategyChangeExecution>(
      ({ proposalId, confirmSensitive = false }) => apiPost<StrategyChangeExecution>(
        strategyProposal(V2_ENDPOINTS.strategyProposalApprove, proposalId),
        { confirm_sensitive: confirmSensitive }),
      { onSuccess: () => refresh() },
    ),
    dismiss: useMutation<string, StrategyChangeProposal>(
      proposalId => apiPost<StrategyChangeProposal>(
        strategyProposal(V2_ENDPOINTS.strategyProposalDismiss, proposalId), {}),
      { onSuccess: () => refresh() },
    ),
  }
}
