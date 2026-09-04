// Ported from webapp/src/components/CopilotPanel.test.tsx.
//
// Only `streamChat` is mocked; `actionEndpoint` stays real so Confirm exercises
// the genuine endpoint resolution, exactly as V1 did. Three cases are new and
// specific to the port: the streamed preview must be dropped in favour of the
// `done` text (pre-gate tokens are never kept); a reply's raw HTML must be
// escaped, which V1 got for free from react-markdown but this port has to
// guarantee in app/utils/markdown.ts because the sink is `v-html`; and the scope
// watcher must reset the thread, because the drawer reuses one panel instance
// across jobs.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises, type VueWrapper } from '@vue/test-utils'
import CopilotPanel from '~/components/CopilotPanel.vue'
import type { ChatDone, ChatEvent } from '~/utils/chat'
import type { Route } from '../support/http'
import { stubFetch } from '../support/http'

const streamChat = vi.hoisted(() => vi.fn())

vi.mock('~/utils/chat', async (importOriginal) => ({
  ...(await importOriginal<typeof import('~/utils/chat')>()),
  streamChat,
}))

function done(text: string, extra: Partial<ChatDone> = {}): ChatEvent {
  return {
    event: 'done',
    data: { text, session_id: null, mandate_ok: true, flags: [], ...extra },
  }
}

/** History + any job POST; both go through the native `fetch` seam. */
function routes(extra: Route[] = []): Route[] {
  return [
    ...extra,
    { match: '/api/chat/history', method: 'GET', json: { messages: [] } },
    { match: '/api/jobs/', method: 'POST', json: { status: 'ok' } },
  ]
}

/** Drive the next turn with a canned event sequence. */
function turn(...events: ChatEvent[]) {
  streamChat.mockImplementationOnce(
    async (_body: unknown, opts: { onEvent: (e: ChatEvent) => void }) => {
      for (const ev of events) opts.onEvent(ev)
    },
  )
}

// `VueWrapper`, not `Awaited<ReturnType<typeof mountSuspended>>`: that resolves
// to `any` through the generic, which unties every helper below from the DOM API.
type Wrapper = VueWrapper

function buttonNamed(wrapper: Wrapper, label: string) {
  const found = wrapper.findAll('button').find(b => b.text() === label)
  if (!found) throw new Error(`no button labelled ${label}`)
  return found
}

async function ask(wrapper: Wrapper, text: string) {
  await wrapper.get('[aria-label="Message copilot"]').setValue(text)
  await buttonNamed(wrapper, 'Ask').trigger('click')
  await flushPromises()
}

describe('CopilotPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    streamChat.mockReset()
    clearNuxtData()
  })

  it('shows the user message and the authoritative done text', async () => {
    turn(
      { event: 'token', data: 'Strea' },
      { event: 'token', data: 'ming…' },
      done('Here is my advice.', { session_id: 's1' }),
    )
    stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel)
    await ask(wrapper, 'what next?')

    expect(wrapper.text()).toContain('what next?')
    expect(wrapper.text()).toContain('Here is my advice.')
  })

  it('drops the streamed preview in favour of the gated done text', async () => {
    // Not a V1 case. The tokens are pre-mandate prose, so keeping them would show
    // the user text the gate may have rewritten or refused.
    let emit!: (ev: ChatEvent) => void
    let release!: () => void
    streamChat.mockImplementationOnce(
      async (_body: unknown, opts: { onEvent: (ev: ChatEvent) => void }) => {
        emit = opts.onEvent
        await new Promise<void>((resolve) => {
          release = resolve
        })
      },
    )
    stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel)
    await ask(wrapper, 'what next?')

    emit({ event: 'token', data: 'Strea' })
    emit({ event: 'token', data: 'ming…' })
    await flushPromises()
    expect(wrapper.get('.copilot-msg.preview').text()).toBe('Streaming…')

    emit(done('Here is my advice.'))
    release()
    await flushPromises()
    expect(wrapper.find('.copilot-msg.preview').exists()).toBe(false)
    expect(wrapper.text()).toContain('Here is my advice.')
  })

  it('renders assistant Markdown — bold and lists, with no literal markup left', async () => {
    turn(done('Try **this** first:\n\n- one\n- two'))
    stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel)
    await ask(wrapper, 'advice?')

    expect(wrapper.get('.copilot-msg.assistant strong').text()).toBe('this')
    expect(wrapper.findAll('.copilot-msg.assistant li')).toHaveLength(2)
    expect(wrapper.text()).not.toContain('**this**')
  })

  it('escapes raw HTML in a reply instead of rendering it', async () => {
    // Not a V1 case, and it has to be one here: react-markdown could not emit raw
    // HTML by construction, `v-html` can, so `html: false` in markdown.ts is the
    // only thing standing between an LLM reply and script injection.
    turn(done('<img src=x onerror="alert(1)"> careful'))
    stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel)
    await ask(wrapper, 'inject?')

    expect(wrapper.find('.copilot-msg.assistant img').exists()).toBe(false)
    expect(wrapper.get('.copilot-msg.assistant').text())
      .toContain('<img src=x onerror="alert(1)">')
  })

  it('renders an action card and POSTs to the typed endpoint on Confirm', async () => {
    turn(
      {
        event: 'action_proposal',
        data: { type: 'set_status', job_id: 7, args: { status: 'applied' }, label: 'Mark as applied' },
      },
      done('Done.'),
    )
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel, { props: { scope: 'job', scopeId: 7 } })
    await ask(wrapper, 'mark it applied')

    expect(wrapper.text()).toContain('Mark as applied')
    await buttonNamed(wrapper, 'Confirm').trigger('click')
    await flushPromises()

    const posts = http.callsTo('/api/jobs/7/status')
    expect(posts.map(c => c.method)).toEqual(['POST'])
    expect(JSON.parse(String(posts[0]!.init?.body))).toEqual({ status: 'applied' })
    // The card is consumed, so a second click cannot re-fire the action. Asserted
    // on the card element, not on its label: the confirmation line the panel
    // appends quotes that same label back.
    expect(wrapper.find('.copilot-action').exists()).toBe(false)
  })

  it('leaves a queued-feedback line in the log after confirming a regen', async () => {
    turn(
      {
        event: 'action_proposal',
        data: { type: 'regen', job_id: 7, args: { notes: 'More NLP' }, label: 'Regenerate CV' },
      },
      done('On it.'),
    )
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel, { props: { scope: 'job', scopeId: 7 } })
    await ask(wrapper, 'regen the cv')

    await buttonNamed(wrapper, 'Confirm').trigger('click')
    await flushPromises()

    // A regen only queues work the background run renders later, so the log says
    // "queued" rather than "done" — the user is told what actually happened.
    expect(wrapper.find('.copilot-action').exists()).toBe(false)
    expect(wrapper.text()).toMatch(/queued/i)
    // `creativity` defaults server-side too, but the client sends it explicitly.
    expect(JSON.parse(String(http.callsTo('/api/jobs/7/regen')[0]!.init?.body)))
      .toEqual({ notes: 'More NLP', creativity: 'balanced' })
  })

  it('echoes the inferred creativity in the regen confirmation line', async () => {
    turn(
      {
        event: 'action_proposal',
        data: {
          type: 'regen',
          job_id: 7,
          args: { notes: 'max JD match', creativity: 'bold' },
          label: 'Regenerate CV',
        },
      },
      done('On it.'),
    )
    stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel, { props: { scope: 'job', scopeId: 7 } })
    // The prompt deliberately omits "bold", so the assertion can only match the
    // confirmation line, never the echoed user message.
    await ask(wrapper, 'tailor this cv aggressively for the JD')

    await buttonNamed(wrapper, 'Confirm').trigger('click')
    await flushPromises()

    // Restating the boldness level shows the copilot understood "be bold".
    expect(wrapper.text()).toContain('(bold)')
  })

  it('dismisses an action card without POSTing', async () => {
    turn(
      { event: 'action_proposal', data: { type: 'skip', job_id: 9, label: 'Skip this role' } },
      done('ok'),
    )
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel, { props: { scope: 'job', scopeId: 9 } })
    await ask(wrapper, 'skip')

    expect(wrapper.text()).toContain('Skip this role')
    await buttonNamed(wrapper, 'Dismiss').trigger('click')
    await flushPromises()

    expect(wrapper.find('.copilot-action').exists()).toBe(false)
    expect(http.called('/skip')).toBe(false)
  })

  it('lets the user edit an action\'s args before confirming', async () => {
    turn(
      {
        event: 'action_proposal',
        data: { type: 'set_status', job_id: 7, args: { status: 'applied' }, label: 'Mark as applied' },
      },
      done('Done.'),
    )
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel, { props: { scope: 'job', scopeId: 7 } })
    await ask(wrapper, 'change status')

    await buttonNamed(wrapper, 'Edit').trigger('click')
    await wrapper.get('[aria-label="Edit action"]').setValue('{"status":"interview"}')
    await buttonNamed(wrapper, 'Confirm').trigger('click')
    await flushPromises()

    expect(JSON.parse(String(http.callsTo('/api/jobs/7/status')[0]!.init?.body)))
      .toEqual({ status: 'interview' })
  })

  it('falls back to the proposed args when the edit is not valid JSON', async () => {
    // Not a V1 case. The textarea is free text, so a half-finished edit must not
    // throw inside the click handler and leave the card stuck.
    turn(
      {
        event: 'action_proposal',
        data: { type: 'set_status', job_id: 7, args: { status: 'applied' }, label: 'Mark as applied' },
      },
      done('Done.'),
    )
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel, { props: { scope: 'job', scopeId: 7 } })
    await ask(wrapper, 'change status')

    await buttonNamed(wrapper, 'Edit').trigger('click')
    await wrapper.get('[aria-label="Edit action"]').setValue('{"status":')
    await buttonNamed(wrapper, 'Confirm').trigger('click')
    await flushPromises()

    expect(JSON.parse(String(http.callsTo('/api/jobs/7/status')[0]!.init?.body)))
      .toEqual({ status: 'applied' })
    expect(wrapper.find('.copilot-action').exists()).toBe(false)
  })

  it('renders an unmapped action but refuses to POST it', async () => {
    // `draft_followup` has no entry in actionEndpoint, so the card must render
    // disabled rather than POST to a 404. V1 asserted the mapping in isolation;
    // this asserts what the user sees.
    turn(
      { event: 'action_proposal', data: { type: 'draft_followup', job_id: 5, label: 'Draft a follow-up' } },
      done('ok'),
    )
    const http = stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel, { props: { scope: 'job', scopeId: 5 } })
    await ask(wrapper, 'follow up')

    expect(wrapper.text()).toContain('Draft a follow-up')
    expect(wrapper.text()).toContain('Not available yet.')
    const confirm = buttonNamed(wrapper, 'Confirm')
    expect(confirm.attributes('disabled')).toBeDefined()
    await confirm.trigger('click')
    await flushPromises()
    expect(http.callsTo('/api/jobs/')).toHaveLength(0)
  })

  it('hydrates the persisted conversation on mount', async () => {
    // The user's complaint in V1: leaving the chat lost the thread. On mount the
    // panel replays GET /api/chat/history for its scope.
    stubFetch(routes([{
      match: '/api/chat/history',
      method: 'GET',
      json: {
        messages: [
          { role: 'user', text: 'earlier question', created_at: '2026-06-16T09:00:00' },
          { role: 'assistant', text: 'earlier answer', created_at: '2026-06-16T09:00:01' },
        ],
      },
    }]))
    const wrapper = await mountSuspended(CopilotPanel, { props: { scope: 'job', scopeId: 42 } })
    await flushPromises()

    expect(wrapper.text()).toContain('earlier question')
    expect(wrapper.text()).toContain('earlier answer')
  })

  it('drops the previous job\'s thread when the scope changes', async () => {
    // Not a V1 case: V1 remounted CopilotPanel per job. The drawer here mounts it
    // without a `key`, so the instance is reused and job 42's thread would bleed
    // into job 43 without the scope watcher's reset.
    stubFetch(routes([
      {
        match: 'scope_id=42',
        method: 'GET',
        json: { messages: [{ role: 'user', text: 'about 42', created_at: '2026-06-16T09:00:00' }] },
      },
      { match: 'scope_id=43', method: 'GET', json: { messages: [] } },
    ]))
    const wrapper = await mountSuspended(CopilotPanel, { props: { scope: 'job', scopeId: 42 } })
    await flushPromises()
    expect(wrapper.text()).toContain('about 42')

    await wrapper.setProps({ scopeId: 43 })
    await flushPromises()
    expect(wrapper.text()).not.toContain('about 42')
  })

  it('warns when the reply fails the mandate gate', async () => {
    turn(done('draft', { mandate_ok: false, flags: ['anonymization_config_missing'] }))
    stubFetch(routes())
    const wrapper = await mountSuspended(CopilotPanel)
    await ask(wrapper, 'draft something')

    const alert = wrapper.get('[role="alert"]')
    expect(alert.text()).toContain('anonymization_config_missing')
    expect(alert.text()).toContain('did not clear the safety gate')
  })
})
