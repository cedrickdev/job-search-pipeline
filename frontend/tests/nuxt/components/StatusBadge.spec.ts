// Ported from webapp/src/components/StatusBadge.test.tsx.
//
// ScoreChip has no V1 test of its own; it is covered here because the two pills
// share `app/utils/status.ts` and the table asserts on the score column's text,
// which is the chip's output.
import { describe, expect, it } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import ScoreChip from '~/components/ScoreChip.vue'
import StatusBadge from '~/components/StatusBadge.vue'

describe('StatusBadge', () => {
  it('renders the status text', async () => {
    const wrapper = await mountSuspended(StatusBadge, { props: { status: 'Ready to apply' } })
    expect(wrapper.text()).toContain('Ready to apply')
  })

  it('drives the badge colour from one custom property', async () => {
    const wrapper = await mountSuspended(StatusBadge, { props: { status: 'Offer' } })
    expect(wrapper.get('span').attributes('style')).toContain('--badge-color: var(--status-offer)')
  })
})

describe('ScoreChip', () => {
  it('renders the score and its tone class', async () => {
    const wrapper = await mountSuspended(ScoreChip, { props: { score: 91 } })
    expect(wrapper.text()).toBe('91')
    expect(wrapper.get('span').classes()).toContain('high')
  })

  it('renders an em dash for an unscored job', async () => {
    const wrapper = await mountSuspended(ScoreChip, { props: { score: null } })
    expect(wrapper.text()).toBe('—')
    expect(wrapper.get('span').classes()).toContain('none')
  })
})
