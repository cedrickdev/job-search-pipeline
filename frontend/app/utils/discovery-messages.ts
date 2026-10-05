/** This delivery translates only Swiss discovery controls, not the whole V2 UI. */
export type DiscoveryLocale = 'fr' | 'en'
export type DiscoveryState = 'IDLE' | 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'PARTIAL' | 'FAILED' | 'ERROR'

const messages: Record<DiscoveryLocale, Record<DiscoveryState, string>> = {
  fr: {
    IDLE: 'Aucune collecte disponible pour cette recherche.',
    QUEUED: 'Recherche en attente de démarrage…',
    RUNNING: 'Recherche des offres en cours…',
    SUCCEEDED: 'Recherche terminée.',
    PARTIAL: 'Recherche incomplète : certaines sources n’ont pas répondu. Les offres précédentes restent visibles.',
    FAILED: 'La recherche a échoué. Les offres précédentes restent visibles.',
    ERROR: 'Statut indisponible. Les offres précédentes restent visibles.',
  },
  en: {
    IDLE: 'No collection available for this search.',
    QUEUED: 'Discovery is waiting to start…',
    RUNNING: 'Searching for opportunities…',
    SUCCEEDED: 'Discovery completed.',
    PARTIAL: 'Discovery is incomplete: some sources did not respond. Previous results remain visible.',
    FAILED: 'Discovery failed. Previous results remain visible.',
    ERROR: 'Status unavailable. Previous results remain visible.',
  },
}

export function discoveryMessage(locale: DiscoveryLocale, state: DiscoveryState): string {
  return messages[locale][state]
}

const healthCopy = {
  fr: {
    HEALTHY: 'Disponible', DEGRADED: 'Dégradée',
    MISCONFIGURED: 'Configuration à vérifier', UNAVAILABLE: 'Indisponible',
    SOURCE_TIMEOUT: 'délai dépassé',
  },
  en: {
    HEALTHY: 'Available', DEGRADED: 'Degraded',
    MISCONFIGURED: 'Check configuration', UNAVAILABLE: 'Unavailable',
    SOURCE_TIMEOUT: 'timed out',
  },
} as const

export function sourceHealthMessage(
  locale: DiscoveryLocale,
  status: 'HEALTHY' | 'DEGRADED' | 'MISCONFIGURED' | 'UNAVAILABLE',
  reason: string | null,
): string {
  const reasonText = reason === 'SOURCE_TIMEOUT' ? healthCopy[locale].SOURCE_TIMEOUT : null
  return [healthCopy[locale][status], reasonText].filter(Boolean).join(' — ')
}

export const discoveryLabels = {
  fr: { refresh: 'Actualiser les offres', updated: 'Dernière collecte', source: 'Source',
    language: 'Langue de la collecte', empty: 'Aucune offre collectée pour cette recherche.' },
  en: { refresh: 'Refresh opportunities', updated: 'Last collection', source: 'Source',
    language: 'Discovery language', empty: 'No collected opportunities for this search.' },
} as const
