import type { CountryPack, CountryPackList } from '~/types/v2'
import { apiGet } from '~/utils/api-client'
import { V2_ENDPOINTS } from '~/utils/endpoints'
import { useApiQuery } from './useApiQuery'

export interface CountryOption {
  value: string
  label: string
}

/** Safe first-paint fallback. The authenticated catalogue replaces it when loaded. */
export const DEFAULT_COUNTRY_OPTIONS: readonly CountryOption[] = [
  { value: 'CH', label: 'Switzerland' },
]

export function useCountryPacksQuery() {
  return useApiQuery<CountryPackList>(
    'country-packs',
    () => apiGet<CountryPackList>(V2_ENDPOINTS.countryPacks),
  )
}

export function countryOptionsFor(catalogue: CountryPackList | null | undefined): CountryOption[] {
  const packs = catalogue?.packs ?? []
  if (packs.length === 0) return [...DEFAULT_COUNTRY_OPTIONS]
  return packs.map(pack => ({ value: pack.country, label: pack.display_name }))
}

export function viewportFor(pack: CountryPack | null | undefined) {
  if (!pack) return null
  return {
    center: [pack.map_viewport.center.longitude, pack.map_viewport.center.latitude] as [number, number],
    zoom: pack.map_viewport.zoom,
  }
}
