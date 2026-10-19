import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const css = (name: string) => readFileSync(resolve('app/assets/css', name), 'utf8')

describe('shared interface foundations', () => {
  it('keeps Nuxt UI surfaces aligned with the application theme', () => {
    const tokens = css('tokens.css')

    expect(tokens).toContain('--ui-border: var(--border)')
    expect(tokens).toContain('--ui-text-muted: var(--text-dim)')
  })

  it('provides visible keyboard focus and reduced-motion behavior globally', () => {
    const base = css('base.css')

    expect(base).toContain(':focus-visible')
    expect(base).toContain('@media (prefers-reduced-motion: reduce)')
  })
})
