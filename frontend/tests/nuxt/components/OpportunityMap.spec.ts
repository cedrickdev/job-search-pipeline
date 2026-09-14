// The one component that owns a live MapLibre map, tested against a fake `maplibre-gl` —
// happy-dom has no WebGL, and the point of §5 is the lifecycle, not the rendering. The fake
// records what the component asks the map to do; the assertions are the §5 contract:
//
//   * the map is built once, on mount, from the runtime style/camera;
//   * on `load` it adds the clustered source and the layers, then reports `ready`;
//   * a point click emits the id, a cluster click zooms, an empty click clears;
//   * new results go through `setData` — the map is never rebuilt;
//   * unmount calls `remove()` exactly once — the WebGL context is dropped;
//   * a construction failure or a style error surfaces as `error`, never a throw (§30).
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { mountSuspended } from '@nuxt/test-utils/runtime'
import { flushPromises } from '@vue/test-utils'
import OpportunityMap from '~/components/map/OpportunityMap.vue'
import { opportunityMarkers, toFeatureCollection } from '~/utils/map-projection'
import { opportunityGeoItem } from '../support/v2-fixtures'

// A fake map that records calls, hoisted so the `vi.mock` factory can hand it back as the
// module's `Map`. It is deliberately dumb: no rendering, just the surface the component uses.
const mapMocks = vi.hoisted(() => {
  interface MockEvent {
    point?: { x: number, y: number }
    features?: { properties: Record<string, unknown> }[]
    error?: { message?: string }
  }
  type Handler = (ev?: MockEvent) => void

  class FakeSource {
    def: unknown
    dataHistory: unknown[] = []
    constructor(def: unknown) { this.def = def }
    setData(data: unknown) { this.dataHistory.push(data) }
    async getClusterExpansionZoom(_id: number) { return 9 }
  }

  const instances: FakeMap[] = []

  class FakeMap {
    opts: Record<string, unknown>
    globalHandlers: Record<string, Handler[]> = {}
    layerHandlers: Record<string, Record<string, Handler[]>> = {}
    sources: Record<string, FakeSource> = {}
    layers: Record<string, unknown>[] = []
    controls: unknown[] = []
    filters: Record<string, unknown> = {}
    easeToCalls: Record<string, unknown>[] = []
    fitBoundsCalls: { box: unknown, opts: unknown }[] = []
    resizeCount = 0
    removed = 0
    queryResult: { properties?: Record<string, unknown>, geometry?: { type: string, coordinates: number[] } }[] = []

    constructor(opts: Record<string, unknown>) {
      this.opts = opts
      // A malformed style URL is how a real MapLibre constructor fails; mimic that.
      if (opts.style === 'boom') throw new Error('bad style')
      instances.push(this)
    }

    on(type: string, a: Handler | string, b?: Handler) {
      if (typeof a === 'function') (this.globalHandlers[type] ??= []).push(a)
      else if (b) ((this.layerHandlers[type] ??= {})[a] ??= []).push(b)
      return this
    }

    fire(type: string, ev?: MockEvent) {
      for (const h of this.globalHandlers[type] ?? []) h(ev)
    }

    fireLayer(type: string, layer: string, ev?: MockEvent) {
      for (const h of this.layerHandlers[type]?.[layer] ?? []) h(ev)
    }

    addControl(control: unknown, _position?: string) { this.controls.push(control); return this }
    addSource(id: string, def: unknown) { this.sources[id] = new FakeSource(def) }
    getSource(id: string): FakeSource | undefined { return this.sources[id] }
    addLayer(def: Record<string, unknown>) { this.layers.push(def) }
    setFilter(id: string, filter: unknown) { this.filters[id] = filter }
    getCanvas() { return { style: {} as Record<string, string> } }
    getCenter() { return { lng: 6.6, lat: 46.5 } }
    getZoom() { return 11 }
    getBounds() {
      return { getNorth: () => 47, getSouth: () => 46, getEast: () => 7, getWest: () => 6 }
    }

    queryRenderedFeatures(_point?: unknown, _opts?: unknown) { return this.queryResult }
    easeTo(opts: Record<string, unknown>) { this.easeToCalls.push(opts) }
    fitBounds(box: unknown, opts: unknown) { this.fitBoundsCalls.push({ box, opts }) }
    resize() { this.resizeCount++ }
    remove() { this.removed++ }

    layerIds() { return this.layers.map(layer => layer.id as string) }
  }

  class FakeControl {
    opts?: unknown
    constructor(opts?: unknown) { this.opts = opts }
  }

  return { FakeMap, FakeControl, instances }
})

vi.mock('maplibre-gl/dist/maplibre-gl.css', () => ({}))
vi.mock('maplibre-gl', () => {
  const api = {
    Map: mapMocks.FakeMap,
    AttributionControl: mapMocks.FakeControl,
    NavigationControl: mapMocks.FakeControl,
  }
  return { ...api, default: api }
})

const { instances } = mapMocks
const STYLE = 'https://tiles.invalid/style.json'
const features = toFeatureCollection(opportunityMarkers([opportunityGeoItem({ id: 'x' })]))

type ExposedMap = { fitTo: (bounds: unknown) => void, resize: () => void }

/** Mount the map and let its async `onMounted` (dynamic import + construction) settle. */
async function mountMap(props: Record<string, unknown> = {}) {
  const wrapper = await mountSuspended(OpportunityMap, {
    props: { features, center: [8.23, 46.82], zoom: 6, styleUrl: STYLE, ...props },
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

/** Fire the map's `load` — what triggers `addLayers` and `ready`. */
async function load(): Promise<void> {
  instances.at(-1)!.fire('load')
  await flushPromises()
}

describe('OpportunityMap · lifecycle', () => {
  beforeEach(() => {
    instances.length = 0
  })

  it('builds one map from the runtime style and camera', async () => {
    await mountMap()

    expect(instances).toHaveLength(1)
    const map = instances[0]!
    expect(map.opts.style).toBe(STYLE)
    expect(map.opts.center).toEqual([8.23, 46.82])
    expect(map.opts.zoom).toBe(6)
    // Attribution is never dropped (§36): the control is added, plus navigation.
    expect(map.controls).toHaveLength(2)
  })

  it('adds the clustered source and every layer on load, then reports ready', async () => {
    const wrapper = await mountMap()
    await load()
    const map = instances[0]!

    const entities = map.getSource('entities')!.def as Record<string, unknown>
    expect(entities.cluster).toBe(true)
    expect(entities.promoteId).toBe('id')
    expect(map.getSource('radius')).toBeDefined()
    // Clusters, their count, the highlight ring and the points all exist.
    expect(map.layerIds()).toEqual(
      expect.arrayContaining(['clusters', 'cluster-count', 'point-highlight', 'points']),
    )
    expect(wrapper.emitted('ready')).toHaveLength(1)
  })

  it('feeds new results through setData — it never rebuilds the map (§5)', async () => {
    const wrapper = await mountMap()
    await load()
    const next = toFeatureCollection(opportunityMarkers([opportunityGeoItem({ id: 'y' })]))

    await wrapper.setProps({ features: next })
    await flushPromises()

    // Still one map, and the new collection arrived by setData on the existing source.
    expect(instances).toHaveLength(1)
    expect(instances[0]!.getSource('entities')!.dataHistory.at(-1)).toEqual(next)
  })

  it('drops the WebGL context exactly once on unmount', async () => {
    const wrapper = await mountMap()
    await load()

    wrapper.unmount()

    expect(instances[0]!.removed).toBe(1)
  })

  it('replays a fit requested before load once the map is ready', async () => {
    // Grab the exposed instance through a ref, exactly as the page does with `mapRef`.
    const mapRef = ref<ExposedMap | null>(null)
    const holder = defineComponent({
      setup() {
        return () => h(OpportunityMap, {
          ref: mapRef,
          features,
          center: [8.23, 46.82],
          zoom: 6,
          styleUrl: STYLE,
        })
      },
    })
    await mountSuspended(holder)
    await flushPromises()
    await flushPromises()

    mapRef.value!.fitTo({ north: 47, south: 46, east: 7, west: 6 })

    // Queued while unloaded — nothing fitted yet.
    expect(instances[0]!.fitBoundsCalls).toHaveLength(0)
    await load()

    expect(instances[0]!.fitBoundsCalls).toHaveLength(1)
  })
})

describe('OpportunityMap · selection', () => {
  beforeEach(() => {
    instances.length = 0
  })

  it('emits the id behind a clicked point', async () => {
    const wrapper = await mountMap()
    await load()

    instances[0]!.fireLayer('click', 'points', { features: [{ properties: { id: 'x' } }] })

    expect(wrapper.emitted('select')).toEqual([['x']])
  })

  it('zooms into a clicked cluster rather than selecting it', async () => {
    const wrapper = await mountMap()
    await load()
    const map = instances[0]!
    map.queryResult = [{ properties: { cluster_id: 1 }, geometry: { type: 'Point', coordinates: [6.6, 46.5] } }]

    map.fireLayer('click', 'clusters', { point: { x: 10, y: 10 } })
    await flushPromises()

    expect(map.easeToCalls).toHaveLength(1)
    expect(map.easeToCalls[0]!.center).toEqual([6.6, 46.5])
    // A cluster click is navigation, not selection.
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('clears the selection when the bare map is clicked', async () => {
    const wrapper = await mountMap()
    await load()
    instances[0]!.queryResult = [] // hit nothing

    instances[0]!.fire('click', { point: { x: 5, y: 5 } })

    expect(wrapper.emitted('select')).toEqual([[null]])
  })

  it('moves the highlight filter to the selected id and eases to it', async () => {
    const wrapper = await mountMap()
    await load()

    await wrapper.setProps({ selectedId: 'x' })
    await flushPromises()

    expect(instances[0]!.filters['point-highlight']).toBeDefined()
    // The default fixture point is [6.6323, 46.5197]; the map eases onto it.
    expect(instances[0]!.easeToCalls.at(-1)!.center).toEqual([6.6323, 46.5197])
  })
})

describe('OpportunityMap · failure is never a throw (§30)', () => {
  beforeEach(() => {
    instances.length = 0
  })

  it('reports a construction failure as an error event, keeping the list alive', async () => {
    const wrapper = await mountMap({ styleUrl: 'boom' })

    expect(instances).toHaveLength(0)
    expect(wrapper.emitted('error')).toHaveLength(1)
  })

  it('surfaces a style-load error once', async () => {
    const wrapper = await mountMap()
    await load()

    instances[0]!.fire('error', { error: { message: 'tiles.json could not be fetched' } })

    expect(wrapper.emitted('error')).toHaveLength(1)
    expect(wrapper.emitted('error')![0]).toEqual(['The map style could not be loaded.'])
  })
})
