<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
const props = defineProps<{ terminal: ReturnType<typeof useClaudeTerminal> }>()
const { t } = useLocale()
const host = ref<HTMLElement | null>(null)
let xterm: Terminal | null = null
let fit: FitAddon | null = null
let observer: ResizeObserver | null = null

function disposeTerminal() {
  observer?.disconnect()
  observer = null
  xterm?.dispose()
  xterm = null
  fit = null
}

function open() {
  if (!host.value || xterm) return
  xterm = new Terminal({ convertEol: true, fontSize: 13, theme: { background: '#0b1020' } })
  fit = new FitAddon(); xterm.loadAddon(fit); xterm.open(host.value); fit.fit()
  xterm.onData(props.terminal.sendInput)
  observer = new ResizeObserver(() => { fit?.fit(); if (xterm) props.terminal.resize(xterm.cols, xterm.rows) })
  observer.observe(host.value)
  connect()
}

function connect() {
  props.terminal.connect(data => xterm?.write(data))
}

function reconnect() {
  props.terminal.disconnect()
  connect()
}
watch(() => props.terminal.connected.value, connected => {
  if (connected && xterm) props.terminal.resize(xterm.cols, xterm.rows)
})
watch(() => props.terminal.session.value?.state, state => {
  if (state === 'ACTIVE') open()
  else disposeTerminal()
}, { flush: 'post' })
onMounted(() => { if (props.terminal.session.value?.state === 'ACTIVE') open() })
onBeforeUnmount(() => { disposeTerminal(); props.terminal.disconnect() })
</script>

<template>
  <section v-if="terminal.session.value?.state === 'ACTIVE'" class="claude-terminal" role="region" :aria-label="t('providers.terminalRegion')">
    <div ref="host" class="claude-terminal__screen" tabindex="0" />
    <div class="claude-terminal__actions">
      <button v-if="!terminal.connected.value" class="btn-ghost" type="button" :aria-label="t('providers.reconnectClaude')" :disabled="terminal.pending.value" @click="reconnect">{{ t('providers.reconnectClaude') }}</button>
      <button class="btn-ghost" type="button" :aria-label="t('providers.stopClaude')" :disabled="terminal.pending.value" @click="terminal.stop().catch(() => {})">{{ t('providers.stopClaude') }}</button>
    </div>
  </section>
</template>

<style scoped>
.claude-terminal { min-width:0; border:1px solid var(--ui-border); border-radius:.75rem; overflow:hidden; background:#0b1020; }
.claude-terminal__screen { height:24rem; height:clamp(16rem, 50dvh, 30rem); min-width:0; padding:.5rem; overflow:hidden; }
.claude-terminal__actions { padding:.65rem; background:var(--ui-surface); display:flex; justify-content:flex-end; }
</style>
