// Cross-component map state — and only that, exactly as ui.ts is scoped.
//
// Backend data is not copied here (the query cache in useGeoExplorer owns it); what lives
// here is the state three sibling components have to agree on and none of them owns:
//
//   * `mode` — opportunities or employers — because the toggle, the map, the list and the
//     query all read it.
//   * `selectedId` — the ONE place a selection lives. A marker click and a list-row click
//     both write it; the map's highlight and the list's highlight both read it. There is
//     no separate "selected marker" and "selected row" to fall out of step, which is the
//     whole point of §12: one source of truth, so the two views cannot disagree.
//   * `selectedSearchProfileId` — which saved search scopes the map, or null for the open
//     reads. It is in the URL too (shareable), but a store field is what the sibling
//     components watch.
//
// Filters (radii, types, remote policy) are deliberately NOT here: the page owns them as
// local reactive state, the way the companies page owns its filter form, because only the
// page and its immediate children read them. Hoisting them would make them survive
// navigation for no benefit (ui.ts makes the same call about the copilot dock).
import { defineStore } from 'pinia'

export type ExplorerMode = 'opportunities' | 'companies'

export const useMapExplorerStore = defineStore('mapExplorer', {
  state: () => ({
    mode: 'opportunities' as ExplorerMode,
    selectedId: null as string | null,
    selectedSearchProfileId: null as string | null,
  }),

  actions: {
    setMode(mode: ExplorerMode) {
      if (mode === this.mode) return
      this.mode = mode
      // An opportunity id is not a company id: a selection from the old mode points at a
      // record the new mode will never show, so it is cleared rather than left dangling.
      this.selectedId = null
    },

    /** Select a record, or `null` to select nothing. Idempotent — no toggle surprise. */
    select(id: string | null) {
      this.selectedId = id
    },

    clearSelection() {
      this.selectedId = null
    },

    setSearchProfile(id: string | null) {
      if (id === this.selectedSearchProfileId) return
      this.selectedSearchProfileId = id
      // A new scope is a new result set; a selection into the old one is stale.
      this.selectedId = null
    },
  },
})
