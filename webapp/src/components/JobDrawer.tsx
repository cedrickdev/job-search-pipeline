import { useState } from "react";
import { useJobDetail, useJobActions, useRunStatus, useTriggerFull } from "../api/hooks";
import { STATUS_ORDER } from "../lib/status";
import { StatusBadge } from "./StatusBadge";
import { ScoreChip } from "./ScoreChip";
import { ConfirmBar } from "./ConfirmBar";
import { FitPanel } from "./FitPanel";
import { PrepTab } from "./PrepTab";
import { CopilotPanel } from "./CopilotPanel";
import "./JobDrawer.css";

type Tab = "overview" | "fit" | "cv" | "cover" | "activity" | "prep" | "copilot";
type Pending = { label: string; run: () => void } | null;

export function JobDrawer({ jobId, onClose }: { jobId: number; onClose: () => void }) {
  const { data, isLoading } = useJobDetail(jobId);
  const actions = useJobActions(jobId);
  const runStatus = useRunStatus();
  const triggerFull = useTriggerFull();
  const [tab, setTab] = useState<Tab>("overview");
  const [pending, setPending] = useState<Pending>(null);

  const ask = (label: string, run: () => void) => () => setPending({ label, run });
  const anyPending =
    actions.go.isPending || actions.applied.isPending || actions.skip.isPending ||
    actions.setStatus.isPending;
  // A full agentic run is the only thing that consumes the regen queue, so a
  // live full run is what turns "queued" into real "regenerating now" progress.
  const fullRunActive =
    runStatus.data?.state === "running" && runStatus.data?.kind === "full";
  // One-click way to actually process a queued/failed regen: kick a full run.
  // Disabled while one is already in flight (or the trigger POST is pending).
  const runNowDisabled = fullRunActive || triggerFull.isPending;
  const RunNowButton = () => (
    <button
      className="btn-primary regen-run-now"
      disabled={runNowDisabled}
      onClick={() => triggerFull.mutate()}
    >
      Run pipeline now
    </button>
  );

  const apply = data?.last_apply ?? null;
  const applyActive =
    apply?.status === "pending" || apply?.status === "in_progress";
  const hasCv = Boolean(data?.cv_versions.en || data?.cv_versions.fr);
  const canApply =
    (data?.application?.status === "Ready to apply" ||
      data?.application?.status === "Approved") &&
    hasCv &&
    !applyActive;

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label="Job detail">
        {isLoading || !data ? (
          <div className="drawer-body">Loading…</div>
        ) : (
          <>
            <div className="drawer-head">
              <h2>{data.job.company}</h2>
              <div className="drawer-sub">{data.job.title} · {data.job.location ?? "—"}</div>
              <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
                {data.application && <StatusBadge status={data.application.status} />}
                <ScoreChip score={data.score?.score ?? null} />
                {/* CV regen lifecycle, most honest state first. A pending request
                    is "queued" until a full run actually consumes it, at which
                    point we say "regenerating now". Once the request is no longer
                    pending, last_regen carries the final outcome — a failure is
                    worth surfacing in the header (done is confirmed on the CV tab). */}
                {data.pending_regen ? (
                  <span
                    className="regen-chip"
                    title={data.pending_regen.notes || undefined}
                    aria-label={fullRunActive ? "CV regenerating now" : "CV regeneration queued"}
                  >
                    {fullRunActive
                      ? "↻ Regenerating now…"
                      : `⏳ CV regen queued (${data.pending_regen.creativity})`}
                  </span>
                ) : data.last_regen?.status === "failed" ? (
                  <span
                    className="regen-chip regen-chip-failed"
                    title={data.last_regen.detail || undefined}
                    aria-label="CV regeneration failed"
                  >
                    ⚠ CV regen failed
                  </span>
                ) : null}
                {/* Manual status override: set any lifecycle status the fixed
                    Approve/Mark applied/Skip transitions cannot reach. The select
                    stays pinned to the current status until the change is confirmed. */}
                <select
                  aria-label="Change status"
                  className="status-select"
                  value={data.application?.status ?? ""}
                  onChange={(e) => {
                    const next = e.target.value;
                    if (next && next !== data.application?.status) {
                      setPending({ label: `Change status to ${next}`, run: () => actions.setStatus.mutate(next) });
                    }
                  }}
                >
                  {!data.application && <option value="" disabled>Set status…</option>}
                  {STATUS_ORDER.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>
            </div>
            <div className="drawer-body">
              {apply && (applyActive ? (
                <div className="apply-banner" role="status">
                  <strong>Applying now…</strong>
                  {apply.channel && <span> ({apply.channel})</span>}
                  <div className="apply-banner-notes">
                    A background worker is submitting this application. This panel
                    updates when it finishes.
                  </div>
                </div>
              ) : apply.status === "applied" ? (
                <div className="apply-banner apply-banner-done" role="status">
                  <strong>✓ Applied</strong>
                  {apply.detail && <span> — {apply.detail}</span>}
                  {apply.screenshot_path && (
                    <div className="apply-banner-shot">Screenshot: {apply.screenshot_path}</div>
                  )}
                </div>
              ) : apply.status === "needs_you" ? (
                <div className="apply-banner apply-banner-warn" role="status">
                  <strong>⚠ Needs you</strong>
                  {apply.detail && <span> — {apply.detail}</span>}
                  <div className="apply-banner-actions">
                    <button
                      className="btn-primary"
                      onClick={ask(`Retry apply to ${data.job.company}`, () =>
                        actions.applyNow.mutate(),
                      )}
                    >
                      Retry
                    </button>
                  </div>
                </div>
              ) : apply.status === "failed" ? (
                <div className="apply-banner apply-banner-failed" role="status">
                  <strong>✕ Failed</strong>
                  {apply.detail && <span> — {apply.detail}</span>}
                  <div className="apply-banner-actions">
                    <button
                      className="btn-primary"
                      onClick={ask(`Retry apply to ${data.job.company}`, () =>
                        actions.applyNow.mutate(),
                      )}
                    >
                      Retry
                    </button>
                  </div>
                </div>
              ) : null)}
              <div className="drawer-tabs">
                {(["overview", "fit", "cv", "cover", "activity", "prep", "copilot"] as Tab[]).map((t) => (
                  <button key={t} className={"drawer-tab" + (tab === t ? " active" : "")} onClick={() => setTab(t)}>
                    {t === "cv" ? "CV" : t[0].toUpperCase() + t.slice(1)}
                  </button>
                ))}
              </div>
              {tab === "overview" && (
                <div>
                  <p>{data.score?.reasoning ?? "No score yet."}</p>
                  <p><a href={data.job.url} target="_blank" rel="noreferrer">Open original posting ↗</a></p>
                  <p style={{ whiteSpace: "pre-wrap", color: "var(--text-dim)" }}>{data.job.description}</p>
                </div>
              )}
              {tab === "fit" && <FitPanel fit={data.fit} />}
              {tab === "cv" && (
                <div>
                  {data.pending_regen ? (
                    <div className="regen-banner" role="status">
                      <strong>
                        {fullRunActive
                          ? `CV regeneration in progress (${data.pending_regen.creativity})`
                          : `CV regeneration queued (${data.pending_regen.creativity})`}
                      </strong>{" "}
                      {fullRunActive
                        ? "— a run is processing it now; the new CV appears here when it finishes."
                        : "— it'll render on the next run, then appear here automatically."}
                      {data.pending_regen.notes && (
                        <div className="regen-banner-notes">“{data.pending_regen.notes}”</div>
                      )}
                      <div className="regen-banner-actions"><RunNowButton /></div>
                    </div>
                  ) : data.last_regen?.status === "failed" ? (
                    <div className="regen-banner regen-banner-failed" role="status">
                      <strong>CV regeneration failed</strong>
                      {data.last_regen.detail && (
                        <div className="regen-banner-notes">{data.last_regen.detail}</div>
                      )}
                      <div className="regen-banner-actions"><RunNowButton /></div>
                    </div>
                  ) : data.last_regen?.status === "done" ? (
                    <div className="regen-banner regen-banner-done" role="status">
                      <strong>CV regeneration applied</strong>
                      {data.last_regen.resolved_at && (
                        <span> {data.last_regen.resolved_at.slice(0, 10)}</span>
                      )}
                    </div>
                  ) : null}
                  {(["en", "fr"] as const).map((lang) => {
                    const cv = data.cv_versions[lang];
                    return cv ? (
                      <p key={lang}>
                        {lang.toUpperCase()} CV — <span className="num">{cv.phone_screen_pct ?? "—"}%</span>{" "}
                        <a href={cv.pdf_url} target="_blank" rel="noreferrer">View PDF ↗</a>
                      </p>
                    ) : <p key={lang} style={{ color: "var(--text-dim)" }}>No {lang.toUpperCase()} CV.</p>;
                  })}
                </div>
              )}
              {tab === "cover" && (
                <div>
                  {data.cover_letter.en
                    ? <pre style={{ whiteSpace: "pre-wrap" }}>{data.cover_letter.en}</pre>
                    : <p style={{ color: "var(--text-dim)" }}>No cover letter drafted.</p>}
                </div>
              )}
              {tab === "activity" && (
                <ul className="drawer-activity">
                  {data.events.length === 0
                    ? <li style={{ color: "var(--text-dim)" }}>No activity yet.</li>
                    : data.events.map((e) => (
                        <li key={e.id}>
                          <span className="activity-when">{e.created_at.slice(0, 10)}</span>{" "}
                          <span className="activity-type">{e.event_type}</span>
                          {e.detail && <span className="activity-detail"> — {e.detail}</span>}
                          <span className="activity-source"> ({e.source})</span>
                        </li>
                      ))}
                </ul>
              )}
              {tab === "prep" && <PrepTab jobId={jobId} />}
              {tab === "copilot" && <CopilotPanel scope="job" scopeId={jobId} />}
            </div>
            {pending && (
              <ConfirmBar
                label={pending.label}
                pending={anyPending}
                onConfirm={() => { pending.run(); setPending(null); }}
                onCancel={() => setPending(null)}
              />
            )}
            <div className="drawer-actions">
              <button className="btn-primary" onClick={ask("Approve " + data.job.company, () => actions.go.mutate())}>Approve</button>
              {canApply && (
                <button
                  className="btn-primary"
                  onClick={ask(`Apply now to ${data.job.company}`, () =>
                    actions.applyNow.mutate(),
                  )}
                >
                  Apply now
                </button>
              )}
              <button className="btn-ghost" onClick={ask("Mark applied", () => actions.applied.mutate())}>Mark applied</button>
              <button className="btn-ghost" onClick={ask("Skip", () => actions.skip.mutate())}>Skip</button>
              <button className="btn-ghost" onClick={onClose}>Close</button>
            </div>
          </>
        )}
      </aside>
    </>
  );
}
