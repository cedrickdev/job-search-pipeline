// Ported from webapp/src/components/MicButton.test.tsx.
//
// `recordClip` is mocked for the same reason V1 mocked it: it opens a real
// MediaRecorder on a real microphone, which no test should do. What is under test
// is the wiring — record, POST the clip to /api/transcribe as multipart, emit the
// text — plus the silent-degradation contract the component header promises.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import MicButton from '~/components/MicButton.vue'
import { stubFetch } from '../support/http'

const recordClip = vi.hoisted(() => vi.fn(async () => new Blob(['audio'], { type: 'audio/webm' })))

vi.mock('~/utils/recorder', () => ({ recordClip }))

describe('MicButton', () => {
  beforeEach(() => {
    recordClip.mockClear()
    recordClip.mockImplementation(async () => new Blob(['audio'], { type: 'audio/webm' }))
  })

  it('records, posts to /api/transcribe, and emits the transcript', async () => {
    const http = stubFetch([{ match: '/api/transcribe', method: 'POST', json: { text: 'spoken words' } }])
    const wrapper = await mountSuspended(MicButton)

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('transcript')).toEqual([['spoken words']])
    expect(http.called('/api/transcribe')).toBe(true)
    // Multipart, not JSON: the backend reads an UploadFile.
    const body = http.callsTo('/api/transcribe')[0]?.init?.body
    expect(body).toBeInstanceOf(FormData)
    expect((body as FormData).get('file')).toBeInstanceOf(Blob)
  })

  it('returns to idle and emits nothing when recording is unavailable', async () => {
    // A denied permission or a browser without MediaRecorder. V1 swallowed it the
    // same way; the point is that the user is left able to type.
    recordClip.mockRejectedValueOnce(new Error('permission denied'))
    stubFetch([])
    const wrapper = await mountSuspended(MicButton)

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('transcript')).toBeUndefined()
    expect(wrapper.get('button').attributes('data-state')).toBe('idle')
  })

  it('returns to idle when transcription fails', async () => {
    stubFetch([{ match: '/api/transcribe', method: 'POST', status: 503, json: { detail: 'no weights' } }])
    const wrapper = await mountSuspended(MicButton)

    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('transcript')).toBeUndefined()
    expect(wrapper.get('button').attributes('data-state')).toBe('idle')
  })
})
