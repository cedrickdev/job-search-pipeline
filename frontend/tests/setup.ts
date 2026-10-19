// Node 22 can expose an undefined localStorage unless a --localstorage-file is
// provided. The browser contract is still part of the frontend test surface, so
// provide the smallest deterministic in-memory implementation for Vitest.
if (typeof globalThis.localStorage === 'undefined') {
  const values = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, String(value)) },
      removeItem: (key: string) => { values.delete(key) },
      clear: () => { values.clear() },
      key: (index: number) => [...values.keys()][index] ?? null,
      get length() { return values.size },
    },
  })
}
