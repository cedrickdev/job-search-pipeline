import { useFollowupActions } from "../api/hooks";
import type { FollowupItem } from "../api/types";
import "./FollowupsPanel.css";

const REASON: Record<FollowupItem["kind"], string> = {
  applied_no_reply: "no reply",
  recruiter_no_outbound: "no outbound",
};

// One row owns its own mutation instances so Draft/Snooze/Dismiss state never
// bleeds between sibling follow-ups.
function FollowupRow({ item, onOpen }: { item: FollowupItem; onOpen: (jobId: number) => void }) {
  const { snooze, dismiss, draft } = useFollowupActions();
  const d = draft.data;

  return (
    <li className="fu-item">
      <div className="fu-head">
        <button type="button" className="fu-main" onClick={() => onOpen(item.job_id)}>
          <span className="job-row-company">{item.company}</span>
          <span className="job-row-title">{item.title}</span>
        </button>
        <span className="ov-when">{item.days}d · {REASON[item.kind]}</span>
      </div>

      <div className="fu-actions">
        <button type="button" onClick={() => draft.mutate(item.job_id)} disabled={draft.isPending}>
          {draft.isPending ? "Drafting…" : "Draft"}
        </button>
        <button type="button" onClick={() => snooze.mutate({ jobId: item.job_id })} disabled={snooze.isPending}>
          Snooze 7d
        </button>
        <button type="button" onClick={() => dismiss.mutate(item.job_id)} disabled={dismiss.isPending}>
          Dismiss
        </button>
      </div>

      {d && (
        <div className="fu-draft">
          {!d.mandate_ok && (
            <p className="fu-warn">
              Draft not verified ({d.flags.join(", ") || "compliance gate"}) — review before sending.
            </p>
          )}
          <p className="fu-subject">{d.subject}</p>
          <pre className="fu-body">{d.body}</pre>
          <button
            type="button"
            className="fu-copy"
            onClick={() => navigator.clipboard?.writeText(`${d.subject}\n\n${d.body}`)}
          >
            Copy
          </button>
        </div>
      )}
    </li>
  );
}

export function FollowupsPanel({ items, onOpen }: {
  items: FollowupItem[];
  onOpen: (jobId: number) => void;
}) {
  return (
    <section className="ov-section" data-testid="followups-due">
      <h2 className="ov-section-title">
        Follow-ups due <span className="ov-count">{items.length}</span>
      </h2>
      {items.length === 0 ? (
        <p className="ov-empty">No follow-ups due.</p>
      ) : (
        <ul className="fu-list">
          {items.map((f) => (
            <FollowupRow key={f.application_id} item={f} onOpen={onOpen} />
          ))}
        </ul>
      )}
    </section>
  );
}
