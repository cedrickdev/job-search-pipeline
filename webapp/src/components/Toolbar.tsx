import { STATUS_ORDER } from "../lib/status";

interface Props {
  q: string; status: string; sort: string;
  onQ: (v: string) => void; onStatus: (v: string) => void; onSort: (v: string) => void;
}

export function Toolbar({ q, status, sort, onQ, onStatus, onSort }: Props) {
  return (
    <div className="toolbar">
      <input
        className="toolbar-input" placeholder="Search company or title…"
        value={q} onChange={(e) => onQ(e.target.value)} aria-label="Search"
      />
      <select value={status} onChange={(e) => onStatus(e.target.value)} aria-label="Status filter">
        <option value="">All statuses</option>
        {STATUS_ORDER.map((s) => <option key={s} value={s}>{s}</option>)}
      </select>
      <select value={sort} onChange={(e) => onSort(e.target.value)} aria-label="Sort">
        <option value="recent">Most recent</option>
        <option value="score">Score</option>
        <option value="company">Company</option>
      </select>
    </div>
  );
}
