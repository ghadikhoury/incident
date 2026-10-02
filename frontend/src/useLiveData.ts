import { useCallback, useEffect, useState } from 'react'
import { api, liveUrl, type Incident, type LiveMessage, type ServiceHealth } from './api'
import { mergeIncidents, upsertIncident } from './logic'

export type Connection = 'connecting' | 'live' | 'offline'

const MAX_RETRY_MS = 10_000

/**
 * Services and incidents, kept up to date by the backend's WebSocket.
 * Reconnects with backoff and reloads incidents after every (re)connect, so
 * nothing is missed while disconnected.
 */
export function useLiveData() {
  const [services, setServices] = useState<ServiceHealth[]>([])
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [connection, setConnection] = useState<Connection>('connecting')
  const [loadError, setLoadError] = useState<string | null>(null)

  const reloadIncidents = useCallback(async () => {
    try {
      const fetched = await api.incidents()
      setIncidents((current) => mergeIncidents(current, fetched))
      setLoadError(null)
    } catch (error) {
      setLoadError(`Could not load incidents: ${(error as Error).message}`)
    }
  }, [])

  useEffect(() => {
    let socket: WebSocket | null = null
    let retryTimer: number | undefined
    let retryMs = 1000
    let stopped = false

    const connect = () => {
      setConnection('connecting')
      socket = new WebSocket(liveUrl())
      socket.onopen = () => {
        retryMs = 1000
        setConnection('live')
        void reloadIncidents()
      }
      socket.onmessage = (event) => {
        const message = JSON.parse(event.data) as LiveMessage
        if (message.type === 'services') setServices(message.data)
        else if (message.type === 'incident')
          setIncidents((current) => upsertIncident(current, message.data))
      }
      socket.onclose = () => {
        if (stopped) return
        setConnection('offline')
        retryTimer = window.setTimeout(connect, retryMs)
        retryMs = Math.min(retryMs * 2, MAX_RETRY_MS)
      }
    }

    connect()
    return () => {
      stopped = true
      window.clearTimeout(retryTimer)
      socket?.close()
    }
  }, [reloadIncidents])

  return { services, incidents, connection, loadError }
}
