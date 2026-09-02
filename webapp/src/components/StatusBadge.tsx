import { statusColor } from "../lib/status";
import "./StatusBadge.css";

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className="status-badge" style={{ ["--badge-color" as string]: statusColor(status) }}>
      <span className="status-dot" />
      {status}
    </span>
  );
}
