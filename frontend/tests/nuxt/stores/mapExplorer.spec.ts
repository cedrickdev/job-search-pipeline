// The store exists for one rule: a selection lives in exactly one place, so the map and the
// list cannot disagree (§12). The cases here are the three ways that single value must be
// cleared to stay honest — a mode change, and a scope change — plus the idempotence that
// keeps a re-click from toggling a selection off by surprise.
//
// Filters are deliberately NOT here (the page owns them); nothing tests for them because
// there is nothing to test.
import { beforeEach, describe, expect, it } from 'vitest'
import { useMapExplorerStore } from '~/stores/mapExplorer'

function store() {
  return useMapExplorerStore()
}

describe('map explorer store', () => {
  beforeEach(() => {
    store().$reset()
  })

  it('opens on opportunities with nothing selected', () => {
    const s = store()
    expect(s.mode).toBe('opportunities')
    expect(s.selectedId).toBeNull()
    expect(s.selectedSearchProfileId).toBeNull()
  })

  it('clears the selection when the mode changes — an opp id is not a company id', () => {
    const s = store()
    s.select('opp-1')
    s.setMode('companies')

    expect(s.mode).toBe('companies')
    expect(s.selectedId).toBeNull()
  })

  it('leaves a selection alone when the mode does not actually change', () => {
    const s = store()
    s.select('opp-1')
    s.setMode('opportunities')

    expect(s.selectedId).toBe('opp-1')
  })

  it('selects idempotently — a second identical select is not a toggle', () => {
    const s = store()
    s.select('opp-1')
    s.select('opp-1')
    expect(s.selectedId).toBe('opp-1')

    s.select(null)
    expect(s.selectedId).toBeNull()
  })

  it('clears the selection when the saved-search scope changes', () => {
    const s = store()
    s.select('opp-1')
    s.setSearchProfile('search-1')

    expect(s.selectedSearchProfileId).toBe('search-1')
    expect(s.selectedId).toBeNull()
  })

  it('does not clear when the same scope is set again', () => {
    const s = store()
    s.setSearchProfile('search-1')
    s.select('opp-1')
    s.setSearchProfile('search-1')

    expect(s.selectedId).toBe('opp-1')
  })

  it('clearSelection is the plain reset of the one selection', () => {
    const s = store()
    s.select('opp-1')
    s.clearSelection()
    expect(s.selectedId).toBeNull()
  })
})
