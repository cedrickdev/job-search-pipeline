interface Props {
  label: string;
  onConfirm: () => void;
  onCancel: () => void;
  pending?: boolean;
}

export function ConfirmBar({ label, onConfirm, onCancel, pending }: Props) {
  return (
    <div className="confirm-bar" role="alertdialog" aria-label="Confirm action">
      <span>{label} — proceed?</span>
      <div>
        <button className="btn-primary" onClick={onConfirm} disabled={pending}>Confirm</button>
        <button className="btn-ghost" onClick={onCancel} disabled={pending}>Cancel</button>
      </div>
    </div>
  );
}
