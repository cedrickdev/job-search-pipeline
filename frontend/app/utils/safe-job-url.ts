/** Public job links are untrusted source data; only absolute web URLs are navigable. */
export function safeJobUrl(value: string | null | undefined): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) {
      return null
    }
    return url.href
  }
  catch {
    return null
  }
}
