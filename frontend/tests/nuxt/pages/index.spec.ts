import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mockNuxtImport, mountSuspended } from '@nuxt/test-utils/runtime'
import IndexPage from '~/pages/index.vue'

const navigateTo = vi.hoisted(() => vi.fn())
mockNuxtImport('navigateTo', () => navigateTo)

describe('root route', () => {
  beforeEach(() => {
    navigateTo.mockReset()
  })

  it('sends an onboarded user to the V2 opportunity map', async () => {
    await mountSuspended(IndexPage)

    expect(navigateTo).toHaveBeenCalledWith('/map')
  })
})
