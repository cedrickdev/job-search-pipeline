import { useState, useEffect } from "react";
import { useSettings, useSaveSettings, useRunStatus, useTriggerDiscovery, useTriggerFull } from "../api/hooks";
import { ApiError } from "../api/client";
import type { Settings, LlmBackend } from "../api/types";
import "./SettingsPage.css";

const CREATIVITY = ["conservative", "balanced", "bold"] as const;
const CADENCE = ["daily", "weekdays"] as const;

const BACKENDS: { value: LlmBackend; label: string }[] = [
  { value: "claude_cli", label: "Local Claude Code (no API key)" },
  { value: "ollama", label: "Ollama (local server)" },
  { value: "lmstudio", label: "LM Studio (local server)" },
];

// Conventional localhost endpoints shown as placeholders when a field is blank.
// These mirror server/chat.py's _BACKEND_DEFAULTS.
const BACKEND_HINTS: Record<string, { url: string; model: string }> = {
  ollama: { url: "http://localhost:11434/v1", model: "llama3.2" },
  lmstudio: { url: "http://localhost:1234/v1", model: "local-model" },
};

export function SettingsPage() {
  const { data, isLoading } = useSettings();
  const save = useSaveSettings();
  const [form, setForm] = useState<Settings | null>(null);
  const { data: status } = useRunStatus();
  const discover = useTriggerDiscovery();
  const full = useTriggerFull();

  useEffect(() => { if (data) setForm(data.settings); }, [data]);

  if (isLoading || !form) return <p>Loading settings…</p>;

  const running = status?.state === "running";
  const runningLabel = status?.started_at
    ? `Running… (started ${status.started_at.slice(11, 16)})`
    : "Running…";

  const set = <K extends keyof Settings>(k: K, v: Settings[K]) =>
    setForm({ ...form, [k]: v });

  const isLocalModel = form.llm_backend !== "claude_cli";
  const hint = BACKEND_HINTS[form.llm_backend];
  const errDetail = save.error instanceof ApiError ? String(save.error.detail) : null;

  return (
    <div className="settings-page">
      <h1>Settings</h1>

      {data?.status === "invalid" && (
        <p className="settings-warn" role="alert">
          Your settings file couldn’t be read; defaults are shown. Saving will rewrite it.
        </p>
      )}

      <section className="settings-card">
        <h2>Auto-apply</h2>
        <label className="settings-row settings-row--check">
          <input
            type="checkbox"
            checked={form.auto_apply}
            onChange={(e) => set("auto_apply", e.target.checked)}
          />
          <span>Enable auto-apply for high-scoring roles</span>
        </label>
        <label className="settings-row">
          <span>Minimum score</span>
          <input
            type="number" min={0} max={100} value={form.auto_apply_min_score}
            onChange={(e) => set("auto_apply_min_score", Number(e.target.value))}
            aria-label="Minimum score"
          />
        </label>
        <label className="settings-row">
          <span>Daily cap</span>
          <input
            type="number" min={1} value={form.auto_apply_daily_cap}
            onChange={(e) => set("auto_apply_daily_cap", Number(e.target.value))}
            aria-label="Daily cap"
          />
        </label>
        <label className="settings-row">
          <span>Tailoring creativity</span>
          <select
            value={form.tailor_creativity}
            onChange={(e) => set("tailor_creativity", e.target.value as Settings["tailor_creativity"])}
            aria-label="Tailoring creativity"
          >
            {CREATIVITY.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
      </section>

      <section className="settings-card">
        <h2>Copilot model</h2>
        <p className="settings-hint">
          Choose which model answers in the copilot. Every option runs on this machine —
          no data leaves your computer and no API key is used.
        </p>
        <label className="settings-row">
          <span>Backend</span>
          <select
            value={form.llm_backend}
            onChange={(e) => set("llm_backend", e.target.value as LlmBackend)}
            aria-label="Copilot backend"
          >
            {BACKENDS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
          </select>
        </label>

        {isLocalModel && (
          <>
            <label className="settings-row">
              <span>Endpoint</span>
              <input
                type="text" value={form.llm_base_url} placeholder={hint?.url}
                onChange={(e) => set("llm_base_url", e.target.value)}
                aria-label="Local endpoint"
              />
            </label>
            <label className="settings-row">
              <span>Model</span>
              <input
                type="text" value={form.llm_model} placeholder={hint?.model}
                onChange={(e) => set("llm_model", e.target.value)}
                aria-label="Model name"
              />
            </label>
            <p className="settings-hint">
              Leave blank to use the default ({hint?.url} · {hint?.model}). The endpoint
              must be local (localhost or a loopback address).
            </p>
          </>
        )}
      </section>

      <section className="settings-card">
        <h2>Daily run schedule</h2>
        <p className="settings-hint">
          When enabled, the full pipeline runs automatically in the background at the
          chosen local time. Replaces the old system scheduler.
        </p>
        <label className="settings-row settings-row--check">
          <input
            type="checkbox"
            checked={form.schedule_enabled}
            onChange={(e) => set("schedule_enabled", e.target.checked)}
          />
          <span>Run the full pipeline automatically on a schedule</span>
        </label>
        <label className="settings-row">
          <span>Time</span>
          <input
            type="time"
            value={form.schedule_time}
            onChange={(e) => set("schedule_time", e.target.value)}
            aria-label="Schedule time"
          />
        </label>
        <label className="settings-row">
          <span>Cadence</span>
          <select
            value={form.schedule_cadence}
            onChange={(e) => set("schedule_cadence", e.target.value as Settings["schedule_cadence"])}
            aria-label="Schedule cadence"
          >
            {CADENCE.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
      </section>

      <section className="settings-card">
        <h2>Run now</h2>
        <p className="settings-hint">
          Sweep the sources and update the jobs database on demand, or kick off the
          full agentic pipeline immediately.
        </p>
        <div className="settings-actions">
          <button
            className="btn-primary"
            disabled={running}
            onClick={() => discover.mutate()}
          >
            {running ? runningLabel : "Run now (discovery)"}
          </button>
          <button
            className="btn-ghost"
            disabled={running}
            onClick={() => full.mutate()}
          >
            Run full pipeline now
          </button>
        </div>
        {status?.last_run && status.state === "idle" && (
          <p
            className={status.last_run.ok ? "settings-ok" : "settings-err"}
            role="status"
          >
            Last {status.last_run.kind} run: {status.last_run.ok ? "ok" : "failed"} — {status.last_run.summary}
          </p>
        )}
      </section>

      <div className="settings-actions">
        <button
          className="btn-primary"
          disabled={save.isPending}
          onClick={() => save.mutate(form)}
        >
          {save.isPending ? "Saving…" : "Save settings"}
        </button>
        {save.isSuccess && !save.isPending && <span className="settings-ok">Saved.</span>}
        {errDetail && <span className="settings-err" role="alert">{errDetail}</span>}
      </div>
    </div>
  );
}
