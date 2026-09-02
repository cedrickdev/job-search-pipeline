import { useState } from "react";
import { useJobs } from "../api/hooks";
import { StatusBadge } from "./StatusBadge";
import { ScoreChip } from "./ScoreChip";
import { Toolbar } from "./Toolbar";
import "./JobsTable.css";

export function JobsTable({ onOpen, track = "job" }: { onOpen: (jobId: number) => void; track?: string }) {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState("recent");
  const { data, isLoading } = useJobs({ q, status, sort });
  const items = (data?.items ?? []).filter((j) => (j.track ?? "job") === track);

  return (
    <div>
      <Toolbar q={q} status={status} sort={sort} onQ={setQ} onStatus={setStatus} onSort={setSort} />
      {isLoading ? (
        <p>Loading…</p>
      ) : items.length === 0 ? (
        <p style={{ color: "var(--text-dim)" }}>No jobs match these filters.</p>
      ) : (
        <table className="jobs-table">
          <thead>
            <tr><th>Company</th><th>Role</th><th>Status</th><th>Score</th><th>Screen %</th></tr>
          </thead>
          <tbody>
            {items.map((j) => (
              <tr key={j.application_id} onClick={() => onOpen(j.job_id)}>
                <td className="cell-company">{j.company}</td>
                <td className="cell-title">{j.title}</td>
                <td><StatusBadge status={j.status} /></td>
                <td><ScoreChip score={j.score} /></td>
                <td className="num">{j.phone_screen_pct ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
