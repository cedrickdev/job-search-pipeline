// Status vocabulary and score tones, ported from webapp/src/lib/status.ts.
//
// The colours resolve to the CSS custom properties in assets/css/tokens.css
// rather than to literals, so a token change moves every badge at once.
export const STATUS_ORDER = [
  'Discovered',
  'Scored',
  'Borderline',
  'Ready to apply',
  'Approved',
  'Applied',
  'Phone Screen',
  'Needs you',
  'Duplicate',
  'Recruiter reply',
  'Interview scheduled',
  'Offer',
  'Rejected',
  'Ghosted',
  'Withdrawn',
  'Archived',
] as const

export type Status = (typeof STATUS_ORDER)[number]

const STATUS_COLORS: Record<string, string> = {
  'Ready to apply': 'var(--status-ready)',
  'Approved': 'var(--status-ready)',
  'Applied': 'var(--status-applied)',
  'Phone Screen': 'var(--status-reply)',
  'Recruiter reply': 'var(--status-reply)',
  'Interview scheduled': 'var(--status-interview)',
  'Offer': 'var(--status-offer)',
  'Rejected': 'var(--status-rejected)',
  'Ghosted': 'var(--status-rejected)',
  'Withdrawn': 'var(--status-neutral)',
}

export function statusColor(status: string): string {
  return STATUS_COLORS[status] ?? 'var(--status-neutral)'
}

export type ScoreTone = 'high' | 'mid' | 'low' | 'none'

export function scoreTone(score: number | null | undefined): ScoreTone {
  if (score === null || score === undefined) return 'none'
  if (score >= 85) return 'high'
  if (score >= 70) return 'mid'
  return 'low'
}
