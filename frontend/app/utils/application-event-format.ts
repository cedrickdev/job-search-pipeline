const KNOWN_DETAILS: Record<string, string> = {
  'this channel has no automated path; a human must apply':
    'Ce canal ne permet pas l’envoi automatique ; vous devez candidater vous-même.',
}

const KNOWN_REASONS: Record<string, string> = {
  UNSUPPORTED_CHANNEL: 'Canal de candidature non automatisé',
}

export function applicationEventDetail(detail: string, locale: 'en' | 'fr'): string {
  if (locale === 'en') return detail
  const opened = /^opened for opportunity:([0-9a-f-]{36}) via MANUAL$/.exec(detail)
  if (opened) return `Candidature ouverte pour l’offre ${opened[1]} par voie manuelle`
  return KNOWN_DETAILS[detail] ?? KNOWN_REASONS[detail] ?? detail
}

export function applicationReason(reason: string, locale: 'en' | 'fr'): string {
  if (locale === 'fr') return KNOWN_REASONS[reason] ?? applicationEventDetail(reason, locale)
  return reason
}
