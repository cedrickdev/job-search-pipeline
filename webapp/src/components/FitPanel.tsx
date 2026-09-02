import type { FitReport } from "../api/types";

export function FitPanel({ fit }: { fit: FitReport }) {
  if (!fit.available)
    return <p style={{ color: "var(--text-dim)" }}>No job description on file — fit analysis unavailable.</p>;
  return (
    <div>
      <p>
        Coverage <span className="num">{Math.round((fit.coverage_score ?? 0) * 100)}%</span>{" "}
        <span className={`fit-tier tier-${fit.risk_tier?.toLowerCase()}`}>{fit.risk_tier}</span>
      </p>
      {fit.missing_keywords && fit.missing_keywords.length > 0 && (
        <p>Missing: {fit.missing_keywords.join(", ")}</p>
      )}
    </div>
  );
}
