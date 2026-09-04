// Client/global UI state — and only that.
//
// The rule for this store is the one from the phase brief: backend data is not
// copied in here. `useAsyncData` already owns a shared, invalidatable cache per
// key, so duplicating jobs or overview payloads into Pinia would create a second
// source of truth with no way to keep them honest.
//
// What is left is the copilot dock, and that is deliberately all. The jobs page's
// view toggle, track tab and open drawer look like store candidates, but in V1
// they were `useState` inside JobsPage, so they reset every time the user
// navigated away and back. Hoisting them here would quietly make them persist —
// arguably nicer, definitely not parity. They stay local to the page.
import { defineStore } from 'pinia'

export const useUiStore = defineStore('ui', {
  state: () => ({
    copilotOpen: false,
  }),

  actions: {
    toggleCopilot() {
      this.copilotOpen = !this.copilotOpen
    },
    closeCopilot() {
      this.copilotOpen = false
    },
  },
})
