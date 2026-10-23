/** HTTPS deployments use the same-origin edge proxy; local development uses the node. */
export function resolveApiBaseUrl(configured: string | undefined, localDefault: string): string {
  const base = (configured || (location.protocol === 'https:' ? '/node' : localDefault)).replace(/\/+$/, '')
  // A plain-HTTP endpoint cannot be fetched from an HTTPS dashboard.
  return location.protocol === 'https:' && /^http:\/\//i.test(base) ? '/node' : base
}
