// Records a single push-to-talk clip. Resolves with the recorded Blob on stop.
// Ported unchanged from webapp/src/lib/recorder.ts — it is plain browser API use
// with no framework surface.
export async function recordClip(stopSignal: Promise<void>): Promise<Blob> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  const recorder = new MediaRecorder(stream)
  const chunks: BlobPart[] = []
  recorder.ondataavailable = e => chunks.push(e.data)
  const done = new Promise<Blob>((resolve) => {
    recorder.onstop = () => {
      stream.getTracks().forEach(t => t.stop())
      resolve(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }))
    }
  })
  recorder.start()
  await stopSignal
  recorder.stop()
  return done
}
