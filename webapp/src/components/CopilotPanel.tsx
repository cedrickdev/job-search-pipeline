import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import { apiPost } from "../api/client";
import { MicButton } from "./MicButton";
import {
  streamChat,
  actionEndpoint,
  fetchChatHistory,
  type ChatEvent,
  type ChatScope,
  type ActionProposal,
  type ChatDone,
} from "../api/chat";
import "./CopilotPanel.css";

interface Msg {
  role: "user" | "assistant";
  text: string;
}

/**
 * Copilot chat panel (spec §6.1/§6.5). Streams the turn over POST /api/chat, shows
 * an ephemeral token preview, then replaces it with the mandate-sanitized `done`
 * text. Copilot-proposed actions render as confirm-gated cards: nothing changes
 * state until the user clicks Confirm, which POSTs to the typed §5.2 endpoint
 * (re-validated server-side). A reply that fails the §6.2 gate is shown with a
 * warning, never silently surfaced as trustworthy.
 */
export function CopilotPanel({ scope = "global", scopeId = 0 }: { scope?: ChatScope; scopeId?: number }) {
  const qc = useQueryClient();
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [preview, setPreview] = useState("");
  const [proposals, setProposals] = useState<ActionProposal[]>([]);
  const [warning, setWarning] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<{ idx: number; text: string } | null>(null);

  // Rehydrate the persisted conversation whenever the scope changes. JobDrawer
  // mounts this panel unkeyed, so we reset every transient bit of UI state first
  // (otherwise the previous job's thread/proposals would bleed through), then
  // replay the stored turns. The clobber guard keeps an in-flight reply the user
  // started before history resolved — and it makes history-less scopes a no-op,
  // so the existing tests that stub an empty body still pass.
  useEffect(() => {
    let cancelled = false;
    setMessages([]);
    setProposals([]);
    setPreview("");
    setWarning(null);
    setError(null);
    fetchChatHistory(scope, scopeId)
      .then((history) => {
        if (cancelled || history.length === 0) return;
        setMessages((cur) =>
          cur.length === 0 ? history.map((m) => ({ role: m.role, text: m.text })) : cur,
        );
      })
      .catch(() => {
        /* no history / fetch failed: start empty */
      });
    return () => {
      cancelled = true;
    };
  }, [scope, scopeId]);

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setMessages((m) => [...m, { role: "user", text: message }]);
    setInput("");
    setPreview("");
    setWarning(null);
    setError(null);
    setBusy(true);
    try {
      await streamChat(
        { message, scope, scope_id: scopeId },
        {
          onEvent: (ev: ChatEvent) => {
            if (ev.event === "token") {
              setPreview((p) => p + String(ev.data ?? ""));
            } else if (ev.event === "action_proposal") {
              setProposals((a) => [...a, ev.data as ActionProposal]);
            } else if (ev.event === "done") {
              const d = ev.data as ChatDone;
              setMessages((m) => [...m, { role: "assistant", text: d.text }]);
              setPreview("");
              if (!d.mandate_ok) setWarning(d.flags ?? []);
            } else if (ev.event === "error") {
              setError(String(ev.data));
            }
          },
        },
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "chat failed");
    } finally {
      setBusy(false);
    }
  }

  async function confirmAction(idx: number, p: ActionProposal) {
    // Fold in any inline edits to the args before resolving the endpoint.
    let effective = p;
    if (editing && editing.idx === idx) {
      try {
        effective = { ...p, args: JSON.parse(editing.text) };
      } catch {
        /* invalid JSON: fall back to the original args */
      }
    }
    const ep = actionEndpoint(effective);
    if (!ep) return;
    await apiPost(ep.path, ep.body);
    setProposals((a) => a.filter((_, i) => i !== idx));
    setEditing(null);
    // Confirm feedback at the trigger point: a regen only queues a request the
    // keyless background run renders later, so say so honestly; other actions
    // apply immediately.
    const what = effective.label || effective.type;
    // Echo the inferred boldness level so the user sees the copilot understood
    // "be bold" / "play it safe" before the keyless run renders the CV.
    const creativity = effective.args?.creativity;
    const how = effective.type === "regen" && typeof creativity === "string" ? ` (${creativity})` : "";
    const note =
      effective.type === "regen"
        ? `✓ ${what}${how} — queued. The new CV will render on the next run.`
        : `✓ ${what} — done.`;
    setMessages((m) => [...m, { role: "assistant", text: note }]);
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["overview"] });
    qc.invalidateQueries({ queryKey: ["job", effective.job_id] });
    qc.invalidateQueries({ queryKey: ["prep", effective.job_id] });
  }

  function dismiss(idx: number) {
    setProposals((a) => a.filter((_, i) => i !== idx));
    if (editing?.idx === idx) setEditing(null);
  }

  return (
    <section className="copilot" aria-label="Copilot">
      <div className="copilot-log">
        {messages.map((m, i) => (
          <div key={i} className={"copilot-msg " + m.role}>
            {/* Assistant prose is Markdown (bold, lists, code); the user's own text stays literal. */}
            {m.role === "assistant" ? (
              <div className="md">
                <ReactMarkdown>{m.text}</ReactMarkdown>
              </div>
            ) : (
              m.text
            )}
          </div>
        ))}
        {busy && preview && (
          <div className="copilot-msg assistant preview">
            <div className="md">
              <ReactMarkdown>{preview}</ReactMarkdown>
            </div>
          </div>
        )}
        {busy && !preview && <div className="copilot-msg assistant preview">…</div>}

        {warning && (
          <div className="copilot-warning" role="alert">
            ⚠ Reply did not clear the safety gate — not saved.
            {warning.length > 0 && <> Flags: {warning.join(", ")}.</>}
          </div>
        )}
        {error && (
          <div className="copilot-warning" role="alert">
            ⚠ {error}
          </div>
        )}

        {proposals.map((p, idx) => {
          const supported = actionEndpoint(p) !== null;
          return (
            <div key={idx} className="copilot-action" role="group" aria-label="Proposed action">
              <div className="copilot-action-label">{p.label || `${p.type} · job ${p.job_id}`}</div>
              {editing?.idx === idx && (
                <textarea
                  className="copilot-action-edit"
                  aria-label="Edit action"
                  value={editing.text}
                  onChange={(e) => setEditing({ idx, text: e.target.value })}
                />
              )}
              {!supported && <div className="copilot-action-note">Not available yet.</div>}
              <div className="copilot-action-btns">
                <button className="btn-primary" disabled={!supported} onClick={() => confirmAction(idx, p)}>
                  Confirm
                </button>
                <button
                  className="btn-ghost"
                  onClick={() =>
                    setEditing(
                      editing?.idx === idx ? null : { idx, text: JSON.stringify(p.args ?? {}, null, 2) },
                    )
                  }
                >
                  Edit
                </button>
                <button className="btn-ghost" onClick={() => dismiss(idx)}>
                  Dismiss
                </button>
              </div>
            </div>
          );
        })}
      </div>

      <div className="copilot-compose">
        <textarea
          aria-label="Message copilot"
          className="copilot-input"
          value={input}
          placeholder="Ask the copilot…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <MicButton onTranscript={(t) => setInput((d) => (d ? d + " " : "") + t)} />
        <button className="btn-primary" disabled={busy || !input.trim()} onClick={() => void send()}>
          Ask
        </button>
      </div>
    </section>
  );
}
