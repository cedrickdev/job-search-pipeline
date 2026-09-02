import { useState } from "react";
import { useJobs, useSetJobStatus } from "../api/hooks";
import { STATUS_ORDER, statusColor } from "../lib/status";
import { ScoreChip } from "./ScoreChip";
import "./Board.css";

export function Board({ onOpen, track = "job" }: { onOpen: (jobId: number) => void; track?: string }) {
  const { data, isLoading } = useJobs({ view: "board" });
  const setStatus = useSetJobStatus();
  // Which card is being dragged, and which column is hovered (for the highlight).
  const [drag, setDrag] = useState<{ jobId: number; status: string } | null>(null);
  const [overCol, setOverCol] = useState<string | null>(null);
  const board = data?.board ?? {};
  // Cards for a status column, restricted to the active track (job vs travail).
  const col = (s: string) => (board[s] ?? []).filter((c) => (c.track ?? "job") === track);
  if (isLoading) return <p>Loading…</p>;

  const total = STATUS_ORDER.reduce((n, s) => n + col(s).length, 0);
  if (total === 0)
    return <p style={{ color: "var(--text-dim)" }}>Rien dans ce pipeline pour l'instant.</p>;

  // Drop a dragged card onto a status column → persist the new status. Every
  // lifecycle column is rendered (even empty ones) so any status is reachable;
  // dropping onto the card's own column is a no-op (no spurious status_change).
  const drop = (status: string) => {
    if (drag && drag.status !== status) {
      setStatus.mutate({ jobId: drag.jobId, status });
    }
    setDrag(null);
    setOverCol(null);
  };

  return (
    <div className="board">
      {STATUS_ORDER.map((status) => (
        <div
          className={"board-col" + (overCol === status ? " drag-over" : "")}
          key={status}
          data-status={status}
          onDragOver={(e) => {
            if (!drag) return;
            e.preventDefault(); // required to make the column a valid drop target
            if (overCol !== status) setOverCol(status);
          }}
          onDragLeave={() => setOverCol((c) => (c === status ? null : c))}
          onDrop={() => drop(status)}
        >
          <div className="board-col-head" style={{ ["--badge-color" as string]: statusColor(status) }}>
            <h3>{status}</h3>
            <span className="board-count">{col(status).length}</span>
          </div>
          {col(status).map((c) => (
            <div
              className="board-card"
              key={c.application_id}
              draggable
              onDragStart={() => setDrag({ jobId: c.job_id, status })}
              onDragEnd={() => { setDrag(null); setOverCol(null); }}
              onClick={() => onOpen(c.job_id)}
            >
              <div className="board-card-company">{c.company}</div>
              <div className="board-card-title">{c.title}</div>
              <div className="board-card-meta">
                <span className="num" style={{ color: "var(--text-dim)" }}>
                  {c.phone_screen_pct !== null ? `${c.phone_screen_pct}% screen` : "—"}
                </span>
                <ScoreChip score={c.score} />
              </div>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
