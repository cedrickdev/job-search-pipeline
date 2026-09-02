import { useRef, useState } from "react";
import { recordClip } from "../lib/recorder";

async function postClip(blob: Blob): Promise<string> {
  const form = new FormData();
  form.append("file", blob, "clip.webm");
  const res = await fetch("/api/transcribe", { method: "POST", body: form });
  if (res.status === 503) throw new Error("voice unavailable");
  if (!res.ok) throw new Error(`transcribe failed: ${res.status}`);
  return (await res.json()).text as string;
}

export function MicButton({ onTranscript }: { onTranscript: (t: string) => void }) {
  const [state, setState] = useState<"idle" | "recording" | "working">("idle");
  const stopRef = useRef<(() => void) | null>(null);

  const toggle = async () => {
    if (state === "recording") {
      stopRef.current?.();
      return;
    }
    if (state !== "idle") return;
    setState("recording");
    const stop = new Promise<void>((resolve) => { stopRef.current = resolve; });
    try {
      const blob = await recordClip(stop);
      setState("working");
      onTranscript(await postClip(blob));
    } catch {
      /* voice unavailable or denied — degrade silently */
    } finally {
      setState("idle");
      stopRef.current = null;
    }
  };

  return (
    <button type="button" className="mic-btn" aria-label="Record voice"
            data-state={state} onClick={toggle}>
      {state === "recording" ? "⏺" : state === "working" ? "…" : "🎤"}
    </button>
  );
}
