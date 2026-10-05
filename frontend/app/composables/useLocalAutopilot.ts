import type { LocalAutopilotReport } from '~/types/v2'
import { apiPost } from '~/utils/api-client'
import { localAutopilot } from '~/utils/endpoints'
import { useMutation } from './useMutation'

export function useLocalAutopilot() {
  return useMutation<string, LocalAutopilotReport>(
    opportunityId => apiPost<LocalAutopilotReport>(localAutopilot(opportunityId)),
  )
}
