import { useAnalytics } from "../api/hooks";
import type { Analytics, DayPoint } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import "./OverviewPage.css";
import "./AnalyticsPage.css";

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

// Dependency-free column chart for a zero-filled daily count series.
function DayBars({ data, accent }: { data: DayPoint[]; accent?: boolean }) {
  const max = Math.max(1, ...data.map((d) => d.count));
  const total = data.reduce((sum, d) => sum + d.count, 0);
  if (total === 0) return <p className="ov-empty">No activity in this window.</p>;
  return (
    <div className="an-cols" role="img" aria-label="daily counts">
      {data.map((d) => (
        <div key={d.date} className="an-col" title={`${d.date}: ${d.count}`}>
          <span
            className={`an-col-fill${accent ? " an-col-accent" : ""}`}
            style={{ height: `${(d.count / max) * 100}%` }}
          />
        </div>
      ))}
    </div>
  );
}

// Phone-screen trend: each point is a daily mean (0–100), colored against the
// target. Sparse — only days with at least one scored CV produce a column.
function PsTrend({ trend }: { trend: Analytics["phone_screen_trend"] }) {
  if (trend.points.length === 0)
    return <p className="ov-empty">No scored CVs in this window.</p>;
  return (
    <div className="an-cols an-cols-tall" role="img" aria-label="phone-screen readiness by day">
      {trend.points.map((p) => (
        <div key={p.date} className="an-col" title={`${p.date}: ${p.value}% (n=${p.n})`}>
          <span
            className={`an-col-fill ${p.value >= trend.target ? "an-col-ok" : "an-col-warn"}`}
            style={{ height: `${Math.min(100, p.value)}%` }}
          />
        </div>
      ))}
    </div>
  );
}

export function AnalyticsPage() {
  const { data, isLoading, error } = useAnalytics();

  if (isLoading) return <p className="ov-state">Loading analytics…</p>;
  if (error || !data) return <p className="ov-state ov-error">Could not load analytics.</p>;

  const an = data;
  const psr = an.kpis.phone_screen_readiness;
  const funnelMax = Math.max(1, ...an.funnel.map((f) => f.count));
  const statusMax = Math.max(1, ...an.status_breakdown.map((s) => s.count));

  return (
    <div className="overview">
      <header className="ov-head">
        <h1>Analytics</h1>
        <p className="kpi-sub">Last {an.days} days</p>
      </header>

      <div className="kpi-grid">
        <KpiTile
          label="Phone-screen readiness"
          value={psr.value === null || psr.value === undefined ? "—" : `${Math.round(psr.value)}%`}
          sub={`target ${psr.target ?? 90}%`}
        />
        <KpiTile label="Response rate" value={asPct(an.kpis.response_rate)} sub="replies ÷ applied" />
        <KpiTile
          label="Velocity"
          value={`${an.kpis.velocity.value}`}
          sub={`goal ${an.kpis.velocity.goal ?? 5} / ${an.kpis.velocity.window_days ?? 7}d`}
        />
      </div>

      <div className="ov-columns">
        <section className="ov-section ov-col">
          <h2 className="ov-section-title">Applications per day</h2>
          <DayBars data={an.applications_per_day} />
        </section>
        <section className="ov-section ov-col">
          <h2 className="ov-section-title">Recruiter replies per day</h2>
          <DayBars data={an.replies_per_day} accent />
        </section>
      </div>

      <section className="ov-section">
        <h2 className="ov-section-title">
          Phone-screen readiness trend
          <span className="ov-count">target {an.phone_screen_trend.target}%</span>
        </h2>
        <PsTrend trend={an.phone_screen_trend} />
      </section>

      <div className="ov-columns">
        <section className="ov-section ov-col">
          <h2 className="ov-section-title">Funnel</h2>
          <div className="ov-bars">
            {an.funnel.map((f) => (
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
            {an.status_breakdown.map((s) => (
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
    </div>
  );
}
