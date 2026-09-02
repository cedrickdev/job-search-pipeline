import { useState } from "react";
import { JobsTable } from "../components/JobsTable";
import { Board } from "../components/Board";
import { JobDrawer } from "../components/JobDrawer";

const TRACKS: { key: string; label: string; hint: string }[] = [
  { key: "job", label: "Jobs étudiants", hint: "Temps partiel · Canton de Vaud" },
  { key: "travail", label: "Travail (dev / remote)", hint: "Laravel · DevOps · à distance" },
];

export function JobsPage() {
  const [view, setView] = useState<"table" | "board">("board");
  const [track, setTrack] = useState<string>("job");
  const [openJob, setOpenJob] = useState<number | null>(null);
  const activeTrack = TRACKS.find((t) => t.key === track) ?? TRACKS[0];
  return (
    <div>
      <div className="track-tabs" role="tablist" aria-label="Type de recherche">
        {TRACKS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={track === t.key}
            className={"track-tab" + (track === t.key ? " active" : "")}
            onClick={() => setTrack(t.key)}
          >
            <span className="track-tab-label">{t.label}</span>
            <span className="track-tab-hint">{t.hint}</span>
          </button>
        ))}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", margin: "16px 0" }}>
        <h1 style={{ margin: 0 }}>{activeTrack.label}</h1>
        <div className="seg">
          <button className={view === "board" ? "active" : ""} onClick={() => setView("board")}>Board</button>
          <button className={view === "table" ? "active" : ""} onClick={() => setView("table")}>Table</button>
        </div>
      </div>
      {view === "board" ? <Board onOpen={setOpenJob} track={track} /> : <JobsTable onOpen={setOpenJob} track={track} />}
      {openJob !== null && <JobDrawer jobId={openJob} onClose={() => setOpenJob(null)} />}
    </div>
  );
}
