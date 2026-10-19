import { describe, expect, it, vi } from 'vitest'
import { mockNuxtImport, mountSuspended } from '@nuxt/test-utils/runtime'
import ReferenceCvImport from '~/components/ReferenceCvImport.vue'

const { addToast } = vi.hoisted(() => ({ addToast: vi.fn() }))
mockNuxtImport('useToast', () => () => ({ add: addToast }))

describe('ReferenceCvImport', () => {
  it('exposes a visible accessible file picker and the Job use case', async () => {
    vi.stubGlobal('$fetch', vi.fn())
    const wrapper = await mountSuspended(ReferenceCvImport)

    const fileInput = wrapper.get<HTMLInputElement>('#reference-cv-file')
    expect(fileInput.attributes('type')).toBe('file')
    expect(fileInput.attributes('aria-describedby')).toBe('reference-cv-file-help')
    expect(wrapper.get('label[for="reference-cv-file"]').text()).toContain('Choose file')
    expect(wrapper.find('[data-testid="reference-cv-purpose"]').exists()).toBe(true)
  })

  it('confirms a successful import with a toast only after the API accepts it', async () => {
    const fetch = vi.fn(async (input: string | URL | Request) => {
      const url = typeof input === 'string' ? input : input.toString()
      if (url.includes('/api/v2/auth/session')) {
        return new Response(JSON.stringify({ error: 'not_authenticated' }), {
          status: 401, headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({ id: 'profile-1' }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetch)
    const wrapper = await mountSuspended(ReferenceCvImport)
    const file = new File(['curriculum vitae'], 'reference.pdf', { type: 'application/pdf' })

    const input = wrapper.get<HTMLInputElement>('#reference-cv-file')
    Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })
    await input.trigger('change')
    await wrapper.get('button.btn-primary').trigger('click')
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(fetch.mock.calls.some(([input]) => String(input).includes('/api/v2/me/reference-cv'))).toBe(true)
    expect(addToast).toHaveBeenCalledWith(expect.objectContaining({
      title: 'Reference CV imported', color: 'success',
    }))
  })
})
