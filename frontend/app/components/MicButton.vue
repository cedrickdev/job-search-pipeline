<!--
  Push-to-talk button for the copilot composer. Ported from
  webapp/src/components/MicButton.tsx.

  Failures degrade silently on purpose: a denied microphone permission, a browser
  without MediaRecorder, or a backend without Whisper weights (503) all leave the
  user typing, which is the normal path anyway. Surfacing an error for a feature
  they may not have asked for would be worse than the feature quietly not being
  there.

  `.mic-btn` has no rules in the ported CSS — it had none in V1 either; the button
  renders with the browser default, and `data-state` is there for the tests and
  for a future style hook.
-->
<script setup lang="ts">
import { ref } from 'vue'
import { apiPostForm } from '~/utils/api-client'
import { ENDPOINTS } from '~/utils/endpoints'
import { recordClip } from '~/utils/recorder'

const emit = defineEmits<{ transcript: [text: string] }>()

type MicState = 'idle' | 'recording' | 'working'

const state = ref<MicState>('idle')
let stopCurrent: (() => void) | null = null

async function postClip(blob: Blob): Promise<string> {
  const form = new FormData()
  form.append('file', blob, 'clip.webm')
  const res = await apiPostForm<{ text: string }>(ENDPOINTS.transcribe, form)
  return res.text
}

async function toggle() {
  if (state.value === 'recording') {
    stopCurrent?.()
    return
  }
  if (state.value !== 'idle') return
  state.value = 'recording'
  const stop = new Promise<void>((resolve) => {
    stopCurrent = resolve
  })
  try {
    const blob = await recordClip(stop)
    state.value = 'working'
    emit('transcript', await postClip(blob))
  } catch {
    /* voice unavailable or denied — degrade silently */
  } finally {
    state.value = 'idle'
    stopCurrent = null
  }
}
</script>

<template>
  <button
    type="button"
    class="mic-btn"
    aria-label="Record voice"
    :data-state="state"
    @click="toggle"
  >
    {{ state === 'recording' ? '⏺' : state === 'working' ? '…' : '🎤' }}
  </button>
</template>
