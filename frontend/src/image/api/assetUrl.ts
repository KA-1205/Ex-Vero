/** Resolve node assets once, even when a view receives an already mapped URL. */
export function resolveAssetUrl(path: string | null | undefined, baseUrl: string): string | null {
  if (!path) return null
  if (/^(https?:\/\/|blob:|data:)/i.test(path)) return path
  const base = baseUrl.replace(/\/+$/, '')
  if (!base || path === base || path.startsWith(`${base}/`)) return path
  return `${base}${path.startsWith('/') ? '' : '/'}${path}`
}
