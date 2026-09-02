import { useNavigate } from "react-router-dom";
import { useOverview } from "../api/hooks";
import type { JobCard } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { ScoreChip } from "../components/ScoreChip";
import { FollowupsPanel } from "../components/FollowupsPanel";
import "./OverviewPage.css";

function asPct(fraction: number | null | undefined): string {
  return fraction === null || fraction === undefined ? "—" : `${Math.round(fraction * 100)}%`;
}

function KpiTile({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="kpi-tile">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      <div className="kpi-sub">{sub}</div>
    </div>
  );
}

function JobRow({ job, onOpen }: { job: JobCard; onOpen: () => void }) {
  return (
    <button type="button" className="job-row" onClick={onOpen}>
      <span className="job-row-main">
        <span className="job-row-company">{job.company}</span>
        <span className="job-row-title">{job.title}</span>
      </span>
      <span className="job-row-meta">
        <ScoreChip score={job.score} />
        <StatusBadge status={job.status} />
      </span>
    </button>
  );
}

function JobSection({ title, jobs, onOpen }: {
  title: string;
  jobs: JobCard[];
  onOpen: (jobId: number) => void;
}) {
  return (
    <section className="ov-section">
      <h2 className="ov-section-title">
        {title} <span className="ov-count">{jobs.length}</span>
      </h2>
      {jobs.length === 0 ? (
        <p className="ov-empty">Nothing here right now.</p>
      ) : (
        <div className="job-list">
          {jobs.map((j) => (
            <JobRow key={j.application_id} job={j} onOpen={() => onOpen(j.job_id)} />
          ))}
        </div>
      )}
    </section>
  );
}

export function OverviewPage() {
  const navigate = useNavigate();
  const { data, isLoading, error } = useOverview();

  if (isLoading) return <p className="ov-state">Loading overview…</p>;
  if (error || !data) return <p className="ov-state ov-error">Could not load overview.</p>;

  const ov = data;
  const openJobs = () => navigate("/jobs");
  const psr = ov.kpis.phone_screen_readiness;
  const funnelMax = Math.max(1, ...ov.funnel.map((f) => f.count));
  const statusMax = Math.max(1, ...ov.status_breakdown.map((s) => s.count));

  return (
    <div className="overview">
      <header className="ov-head">
        <h1>Overview</h1>
      </header>

      <div className="kpi-grid">
        <KpiTile
          label="Phone-screen readiness"
          value={psr.value === null || psr.value === undefined ? "—" : `${Math.round(psr.value)}%`}
          sub={`target ${psr.target ?? 90}%`}
        />
        <KpiTile label="Response rate" value={asPct(ov.kpis.response_rate)} sub="replies ÷ applied" />
        <KpiTile label="Velocity" value={`${ov.kpis.velocity.value}`} sub={`goal ${ov.kpis.velocity.goal ?? 5} / 7d`} />
        <KpiTile label="In flight" value={`${ov.kpis.in_flight}`} sub="active applications" />
        <KpiTile label="Replies to action" value={`${ov.kpis.replies_to_action}`} sub="need a response" />
      </div>

      <div className="ov-columns">
        <div className="ov-col">
          <JobSection title="Today — do these first" jobs={ov.today} onOpen={openJobs} />
          <JobSection title="Borderline review" jobs={ov.borderline} onOpen={openJobs} />
          <JobSection title="Auto-approved today" jobs={ov.auto_approved_today} onOpen={openJobs} />
        </div>

        <div className="ov-col">
          <JobSection title="Recruiter replies" jobs={ov.replies_to_action} onOpen={openJobs} />

          <section className="ov-section">
            <h2 className="ov-section-title">
              Upcoming interviews <span className="ov-count">{ov.upcoming_interviews.length}</span>
            </h2>
            {ov.upcoming_interviews.length === 0 ? (
              <p className="ov-empty">No interviews scheduled.</p>
            ) : (
              <div className="job-list">
                {ov.upcoming_interviews.map((iv) => (
                  <button type="button" key={iv.id} className="job-row" onClick={openJobs}>
                    <span className="job-row-main">
                      <span className="job-row-company">{iv.company}</span>
                      <span className="job-row-title">{iv.round_label} · {iv.title}</span>
                    </span>
                    <span className="job-row-meta">
                      <span className="ov-when">{iv.scheduled_for ?? "TBD"}</span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </section>

          <FollowupsPanel items={ov.followups_due} onOpen={openJobs} />
        </div>
      </div>

      <div className="ov-columns">
        <section className="ov-section ov-col">
          <h2 className="ov-section-title">Funnel</h2>
          <div className="ov-bars">
            {ov.funnel.map((f) => (
              <div key={f.stage} className="ov-bar-row">
                <span className="ov-bar-label">{f.stage}</span>
                <span className="ov-bar-track">
                  <span className="ov-bar-fill" style={{ width: `${(f.count / funnelMax) * 100}%` }} />
                </span>
                <span className="ov-bar-val">
                  {f.count}{f.pct !== null ? ` · ${Math.round(f.pct * 100)}%` : ""}
                </span>
              </div>
            ))}
          </div>
        </section>

        <section className="ov-section ov-col">
          <h2 className="ov-section-title">Pipeline by status</h2>
          <div className="ov-bars">
            {ov.status_breakdown.map((s) => (
              <div key={s.status} className="ov-bar-row">
                <span className="ov-bar-label"><StatusBadge status={s.status} /></span>
                <span className="ov-bar-track">
                  <span
                    className="ov-bar-fill ov-bar-accent"
                    style={{ width: `${(s.count / statusMax) * 100}%` }}
                  />
                </span>
                <span className="ov-bar-val">{s.count}</span>
              </div>
            ))}
          </div>
        </section>
      </div>

      <section className="ov-section">
        <h2 className="ov-section-title">Source health</h2>
        {ov.source_health.length === 0 ? (
          <p className="ov-empty">No source data yet.</p>
        ) : (
          <div className="ov-sources">
            {ov.source_health.map((s) => (
              <div key={s.source} className="ov-source">
                <span className="ov-source-name">{s.source}</span>
                <span className="ov-source-stats">
                  {Object.entries(s)
                    .filter(([k]) => k !== "source")
                    .map(([k, v]) => (
                      <span key={k} className="ov-source-stat">
                        {k.replace(/_/g, " ")}: {String(v)}
                      </span>
                    ))}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
