/** Open an optional live event stream without taking down its page when unavailable. */
export function openEventSocket(baseUrl: string, path: string): WebSocket | null {
  try {
    const url = new URL(`${baseUrl.replace(/\/+$/, '')}${path}`)
    if (location.protocol === 'https:' && url.protocol === 'ws:') {
      console.warn('Live events require a secure wss:// endpoint on HTTPS pages.')
      return null
    }
    return new WebSocket(url)
  } catch (error) {
    console.warn('Live events unavailable', error)
    return null
  }
}
