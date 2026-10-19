// The career-chat control plane's transport and its two pure display helpers.
//
// The turn endpoint (`POST /chat/conversations/{id}/messages`) answers with an SSE
// stream, not JSON, so it cannot go through `api-client.ts` — that helper buffers the
// whole body and parses it as one document. This reads the body as a stream instead,
// exactly as the V1 copilot does, and reuses `parseSseBuffer` (the most-tested piece
// of the V1 frontend) to frame it. Each `data:` payload is one `ChatStreamEvent`.
//
// The rule the phase exists for shapes what this file does and does not do. It streams
// prose and surfaces typed proposals, but it never acts: prose has zero authority, and
// a proposal is inert until a human confirms it on its own route (`api-client.ts`
// POSTs the confirm). So `describeChatAction` renders a proposal from its *typed*
// fields only — never free text a model wrote — and `navigationRoute` maps a confirmed
// `NAVIGATE` to a route this app owns, ignoring any target it does not recognise, which
// is the client half of "navigation is a hint, not an open redirect"
// (docs/CAREER_CHAT.md, api.d.ts NavigationTarget).
import type {
  ChatAction,
  ChatStreamEvent,
  ConversationScope,
  NavigationTarget,
} from '~/types/v2'
import { ApiError, CSRF_HEADER, csrfToken } from '~/utils/api-client'
import { parseSseBuffer } from '~/utils/chat'

/**
 * Stream one user turn, invoking `onEvent` for each `ChatStreamEvent` the route emits.
 *
 * The ownership 404 and the empty-message 422 are raised *before* the stream opens, so
 * they arrive as an ordinary JSON error body rather than an `ERROR` event; this parses
 * that body and throws the same `ApiError` every other call does, so a caller branches
 * on `error.code` here exactly as it does elsewhere. A provider fault that happens
 * *mid-turn* is a terminal `ERROR` event on the stream, not a throw.
 */
export async function streamChatTurn(
  path: string,
  text: string,
  opts: { onEvent: (event: ChatStreamEvent) => void, signal?: AbortSignal },
): Promise<void> {
  const token = csrfToken()
  const res = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(token === null ? {} : { [CSRF_HEADER]: token }),
    },
    body: JSON.stringify({ text }),
    signal: opts.signal,
  })
  if (!res.ok || !res.body) {
    throw await errorFrom(res)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const { events, rest } = parseSseBuffer(buffer)
    buffer = rest
    for (const event of events) opts.onEvent(event.data as ChatStreamEvent)
  }
  // Flush a final frame that arrived without a trailing blank line.
  const { events } = parseSseBuffer(`${buffer}\n\n`)
  for (const event of events) opts.onEvent(event.data as ChatStreamEvent)
}

/** Turn a non-2xx turn response into the `ApiError` the rest of the app throws. */
async function errorFrom(res: Response): Promise<ApiError> {
  let body: unknown = null
  try {
    body = JSON.parse(await res.text())
  }
  catch {
    /* non-JSON or empty error body */
  }
  const detail
    = body && typeof body === 'object' && 'detail' in body
      ? (body as { detail: unknown }).detail
      : body
  return new ApiError(res.status, detail ?? `HTTP ${res.status}`, body)
}

/**
 * A one-line, human-readable label for a proposed action — from its typed fields only.
 *
 * Never the model's own prose: the whole point of the control plane is that what a card
 * offers is derived from the validated `ChatAction`, so a summary a model wrote can
 * describe but never *widen* what confirming it will do. Every branch reads a typed,
 * domain-safe field (a target, a radius, an id), which is why this is safe to render.
 */
export function describeChatAction(action: ChatAction, locale: 'en' | 'fr' = 'en'): string {
  const french = locale === 'fr'
  switch (action.kind) {
    case 'NAVIGATE':
      return french
        ? `Accéder à ${screenName(action.target, locale)}`
        : `Go to ${screenName(action.target, locale)}`
    case 'OPEN_INTERVIEW_PREP':
      return french ? 'Préparer un entretien pour cette offre' : 'Open interview preparation for this opportunity'
    case 'GENERATE_RESUME':
      return action.target_language
        ? french
          ? `Adapter un CV à cette offre en ${action.target_language}`
          : `Tailor a résumé for this opportunity in ${action.target_language}`
        : french ? 'Adapter un CV à cette offre' : 'Tailor a résumé for this opportunity'
    case 'GENERATE_COVER_LETTER':
      return action.target_language
        ? french
          ? `Rédiger une lettre de motivation pour cette offre en ${action.target_language}`
          : `Write a cover letter for this opportunity in ${action.target_language}`
        : french ? 'Rédiger une lettre de motivation pour cette offre' : 'Write a cover letter for this opportunity'
    case 'CREATE_APPLICATION':
      return french ? 'Créer une candidature pour cette offre' : 'Open an application for this opportunity'
    case 'PREPARE_APPLICATION':
      return french ? 'Préparer cette candidature' : 'Prepare this application'
    case 'APPROVE_APPLICATION':
      return french ? 'Approuver cette candidature' : 'Approve this application'
    case 'SUBMIT_APPLICATION':
      return french ? 'Envoyer cette candidature' : 'Submit this application'
    case 'CANCEL_APPLICATION':
      return french ? 'Annuler cette candidature' : 'Cancel this application'
    case 'SET_SEARCH_RADIUS':
      return french
        ? `Définir le rayon de recherche à ${action.radius_km} km`
        : `Set the search radius to ${action.radius_km} km`
    case 'UPDATE_SEARCH_KEYWORDS':
      return french
        ? 'Modifier les mots-clés de cette recherche enregistrée'
        : 'Update the keywords on this saved search'
  }
}

/** A `NavigationTarget` as a sentence fragment, for the action label above. */
function screenName(target: NavigationTarget, locale: 'en' | 'fr'): string {
  return (locale === 'fr' ? NAVIGATION_LABELS_FR : NAVIGATION_LABELS)[target] ?? target
}

const NAVIGATION_LABELS: Record<NavigationTarget, string> = {
  OPPORTUNITIES: 'the opportunity map',
  APPLICATIONS: 'your applications',
  DOCUMENTS: 'your documents',
  MATCHES: 'your matches',
  COMPANIES: 'the company directory',
  INTERVIEW_PREP: 'interview preparation',
  SETTINGS: 'settings',
}

const NAVIGATION_LABELS_FR: Record<NavigationTarget, string> = {
  OPPORTUNITIES: 'la carte des offres',
  APPLICATIONS: 'vos candidatures',
  DOCUMENTS: 'vos documents',
  MATCHES: 'vos correspondances',
  COMPANIES: 'l’annuaire des entreprises',
  INTERVIEW_PREP: 'la préparation aux entretiens',
  SETTINGS: 'vos paramètres',
}

/**
 * The route a confirmed `NAVIGATE` should take the client to, or null to ignore it.
 *
 * Closed on purpose: the model names a member of a fixed set, and this maps only the
 * members this app owns a route for. `MATCHES` and `INTERVIEW_PREP` have no standalone
 * V2 route yet, so they resolve to null and the client stays put — the schema's "a
 * target the client does not recognise is simply ignored, never followed" (api.d.ts
 * NavigationTarget), which keeps a chat action from ever being an open redirect.
 */
export function navigationRoute(target: NavigationTarget): string | null {
  return NAVIGATION_ROUTES[target] ?? null
}

const NAVIGATION_ROUTES: Partial<Record<NavigationTarget, string>> = {
  OPPORTUNITIES: '/map',
  APPLICATIONS: '/applications',
  DOCUMENTS: '/documents',
  COMPANIES: '/companies',
  SETTINGS: '/settings',
}

/**
 * A short label for a thread's domain scope, or null for a `GLOBAL` thread.
 *
 * A `GLOBAL` thread spans the whole account and needs no badge, so it resolves to null
 * and the panel shows nothing. Every anchored scope resolves to a one-word noun the
 * header renders beside the title, so a person can see at a glance that this thread is
 * bound to a single opportunity, application, employer or saved search — the same wall
 * the server enforces, surfaced (docs/CAREER_CHAT.md). This is display only; it grants
 * nothing, exactly as `describeChatAction` describes but never widens.
 */
export function scopeLabel(scope: ConversationScope, locale: 'en' | 'fr' = 'en'): string | null {
  return (locale === 'fr' ? SCOPE_LABELS_FR : SCOPE_LABELS)[scope]
}

const SCOPE_LABELS: Record<ConversationScope, string | null> = {
  GLOBAL: null,
  OPPORTUNITY: 'This opportunity',
  APPLICATION: 'This application',
  COMPANY: 'This employer',
  SEARCH_PROFILE: 'This saved search',
}

const SCOPE_LABELS_FR: Record<ConversationScope, string | null> = {
  GLOBAL: null,
  OPPORTUNITY: 'Cette offre',
  APPLICATION: 'Cette candidature',
  COMPANY: 'Cette entreprise',
  SEARCH_PROFILE: 'Cette recherche enregistrée',
}
