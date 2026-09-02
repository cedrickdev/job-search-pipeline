import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, apiPut } from "./client";
import type { Analytics, FollowupDraft, Overview, JobsResponse, JobDetail, PrepData, RunStatus, Settings, SettingsResponse } from "./types";

export function useSettings() {
  return useQuery({ queryKey: ["settings"], queryFn: () => apiGet<SettingsResponse>("/api/settings") });
}

export function useSaveSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (s: Settings) => apiPut<SettingsResponse>("/api/settings", s),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }),
  });
}

export function useOverview() {
  return useQuery({ queryKey: ["overview"], queryFn: () => apiGet<Overview>("/api/overview") });
}

export function useAnalytics(days = 30) {
  return useQuery({
    queryKey: ["analytics", days],
    queryFn: () => apiGet<Analytics>(`/api/analytics?days=${days}`),
  });
}

export function useJobs(params: { status?: string; q?: string; sort?: string; view?: string }) {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => v && qs.set(k, v));
  const query = qs.toString();
  return useQuery({
    queryKey: ["jobs", params],
    queryFn: () => apiGet<JobsResponse>(`/api/jobs${query ? `?${query}` : ""}`),
  });
}

export function useJobDetail(jobId: number | null) {
  return useQuery({
    queryKey: ["job", jobId],
    enabled: jobId !== null,
    queryFn: () => apiGet<JobDetail>(`/api/jobs/${jobId}`),
    // While a CV regen is queued, poll so the drawer clears the "regenerating"
    // state and shows the new CV as soon as the background run renders it
    // (mirrors useRunStatus' active-only polling). Also poll while an apply is
    // in flight (pending/in_progress) so the banner settles. Idle: no polling.
    refetchInterval: (query) => {
      const d = query.state.data;
      const applyActive =
        d?.last_apply?.status === "pending" ||
        d?.last_apply?.status === "in_progress";
      return d?.pending_regen || applyActive ? 10000 : false;
    },
  });
}

export function usePrep(jobId: number) {
  return useQuery({ queryKey: ["prep", jobId], queryFn: () => apiGet<PrepData>(`/api/jobs/${jobId}/prep`) });
}

export function useGeneratePrep(jobId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost<PrepData>(`/api/jobs/${jobId}/prep/generate`),
    // Show the exact draft the server returned (including an unverified, NOT
    // cached draft) rather than refetching GET — which for a fail-closed draft
    // would return the old/empty cached prep and drop the warning.
    onSuccess: (data) => qc.setQueryData(["prep", jobId], data),
  });
}

export function usePrepMutations(jobId: number) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["prep", jobId] });
  return {
    saveNotes: useMutation({ mutationFn: (notes_md: string) => apiPut(`/api/jobs/${jobId}/prep/notes`, { notes_md }), onSuccess: invalidate }),
    addInterview: useMutation({ mutationFn: (b: { round_label: string; scheduled_for?: string }) => apiPost(`/api/jobs/${jobId}/interviews`, b), onSuccess: invalidate }),
  };
}

export function useFollowupActions() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["overview"] });
  return {
    snooze: useMutation({
      mutationFn: ({ jobId, days }: { jobId: number; days?: number }) =>
        apiPost(`/api/jobs/${jobId}/followup/snooze`, { days: days ?? 7 }),
      onSuccess: invalidate,
    }),
    dismiss: useMutation({
      mutationFn: (jobId: number) => apiPost(`/api/jobs/${jobId}/followup/dismiss`),
      onSuccess: invalidate,
    }),
    draft: useMutation({
      mutationFn: (jobId: number) =>
        apiPost<FollowupDraft>(`/api/jobs/${jobId}/draft_followup`),
    }),
  };
}

export function useJobActions(jobId: number | null) {
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["overview"] });
    if (jobId !== null) qc.invalidateQueries({ queryKey: ["job", jobId] });
  };
  return {
    go: useMutation({ mutationFn: () => apiPost(`/api/jobs/${jobId}/go`), onSuccess: invalidate }),
    applied: useMutation({ mutationFn: () => apiPost(`/api/jobs/${jobId}/applied`, {}), onSuccess: invalidate }),
    skip: useMutation({ mutationFn: () => apiPost(`/api/jobs/${jobId}/skip`), onSuccess: invalidate }),
    setStatus: useMutation({ mutationFn: (status: string) => apiPost(`/api/jobs/${jobId}/status`, { status }), onSuccess: invalidate }),
    regen: useMutation({ mutationFn: (notes: string) => apiPost(`/api/jobs/${jobId}/regen`, { notes }), onSuccess: invalidate }),
    applyNow: useMutation({
      mutationFn: () => apiPost(`/api/jobs/${jobId}/apply-now`, {}),
      onSuccess: invalidate,
    }),
  };
}

// Board drag-and-drop status change. Unlike useJobActions (bound to one job),
// this is parameterized by jobId so a single board-level mutation serves every
// card. Hits the same manual-status endpoint and refreshes the board + overview.
export function useSetJobStatus() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ jobId, status }: { jobId: number; status: string }) =>
      apiPost(`/api/jobs/${jobId}/status`, { status }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["overview"] });
    },
  });
}

// Poll run status only while a run is active (every 2s); stop polling at idle.
export function useRunStatus() {
  return useQuery({
    queryKey: ["runStatus"],
    queryFn: () => apiGet<RunStatus>("/api/runs/status"),
    refetchInterval: (query) =>
      query.state.data?.state === "running" ? 2000 : false,
  });
}

// Manual discovery sweep. On success, refresh status and the job board.
export function useTriggerDiscovery() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost("/api/runs/discover"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["runStatus"] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["overview"] });
    },
  });
}

// Manual full (agentic) pipeline run. Only run status changes synchronously.
export function useTriggerFull() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => apiPost("/api/runs/full"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runStatus"] }),
  });
}
