import { useState, useEffect } from "react";
import { usePrep, usePrepMutations, useGeneratePrep } from "../api/hooks";
import "./PrepTab.css";

export function PrepTab({ jobId }: { jobId: number }) {
  const { data, isLoading } = usePrep(jobId);
  const { saveNotes, addInterview } = usePrepMutations(jobId);
  const generate = useGeneratePrep(jobId);
  const [notes, setNotes] = useState("");
  const [round, setRound] = useState("");
  const [when, setWhen] = useState("");

  useEffect(() => { if (data) setNotes(data.notes_md); }, [data]);

  if (isLoading || !data) return <p>Loading prep…</p>;

  // A fresh draft that failed the §6.2 gate: surfaced (so the user sees it) but
  // not saved server-side, hence flagged here rather than treated as ready.
  const unverified = data.mandate_ok === false;

  return (
    <div>
      <div className="prep-section prep-generate">
        <button className="btn-primary" disabled={generate.isPending}
                onClick={() => generate.mutate()}>
          {generate.isPending ? "Generating…" : "Generate prep"}
        </button>
        {data.generated_at && (
          <span className="prep-generated-at">
            Generated {data.generated_at.slice(0, 16).replace("T", " ")}
          </span>
        )}
      </div>

      {unverified && (
        <div className="prep-warning" role="alert">
          ⚠ This draft did not clear the safety gate and was not saved.
          {data.flags && data.flags.length > 0 && <> Flags: {data.flags.join(", ")}.</>}
        </div>
      )}

      <div className="prep-section">
        <h4>Notes</h4>
        <textarea
          className="prep-notes"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          onBlur={() => { if (notes !== data.notes_md) saveNotes.mutate(notes); }}
          aria-label="Prep notes"
        />
      </div>

      {data.likely_questions && data.likely_questions.length > 0 && (
        <div className="prep-section">
          <h4>Likely questions</h4>
          <ul className="prep-list">{data.likely_questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
        </div>
      )}
      {data.talking_points && data.talking_points.length > 0 && (
        <div className="prep-section">
          <h4>Talking points</h4>
          <ul className="prep-list">{data.talking_points.map((q, i) => <li key={i}>{q}</li>)}</ul>
        </div>
      )}
      {data.company_research && data.company_research.length > 0 && (
        <div className="prep-section">
          <h4>Company research</h4>
          <ul className="prep-list">{data.company_research.map((q, i) => <li key={i}>{q}</li>)}</ul>
        </div>
      )}

      <div className="prep-section">
        <h4>Interview log</h4>
        <ul className="prep-list">
          {data.interviews.map((iv) => (
            <li key={iv.id} className="iv-row">
              <span>{iv.round_label}{iv.scheduled_for ? ` · ${iv.scheduled_for.slice(0, 10)}` : ""}</span>
              <span style={{ color: "var(--text-dim)" }}>{iv.outcome ?? "scheduled"}</span>
            </li>
          ))}
        </ul>
        <div className="iv-add">
          <input placeholder="Round (e.g. Phone screen)" value={round} onChange={(e) => setRound(e.target.value)} aria-label="Round label" />
          <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} aria-label="Scheduled for" />
          <button className="btn-ghost" disabled={!round} onClick={() => {
            addInterview.mutate({ round_label: round, scheduled_for: when || undefined });
            setRound(""); setWhen("");
          }}>Add</button>
        </div>
      </div>
    </div>
  );
}
