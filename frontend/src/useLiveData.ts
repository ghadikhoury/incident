import { useCallback, useEffect, useState } from 'react'
import { api, liveUrl, type Incident, type LiveMessage, type ServiceHealth, type ServiceNode } from './api'
import { mergeIncidents, upsertIncident } from './logic'

export type Connection = 'connecting' | 'live' | 'offline'

const MAX_RETRY_MS = 10_000

/**
 * Fetches the initial state over REST, then keeps it fresh with WebSocket pushes.
 * Reconnects with backoff and reloads after every (re)connect.
 */
export function useLiveData() {
  const [services, setServices] = useState<ServiceHealth[]>([])
  const [graph, setGraph] = useState<ServiceNode[]>([])
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [connection, setConnection] = useState<Connection>('connecting')
  const [incidentError, setIncidentError] = useState<string | null>(null)
  const [serviceError, setServiceError] = useState<string | null>(null)
  const [graphError, setGraphError] = useState<string | null>(null)

  const reloadIncidents = useCallback(async () => {
    try {
      const fetched = await api.incidents()
      setIncidents((current) => mergeIncidents(current, fetched))
      setIncidentError(null)
    } catch (error) {
      setIncidentError(`Could not load incidents: ${(error as Error).message}`)
    }
  }, [])

  const reloadServices = useCallback(async () => {
    try {
      setServices(await api.services())
      setServiceError(null)
    } catch (error) {
      setServiceError(`Could not load services: ${(error as Error).message}`)
    }
  }, [])

  const reloadGraph = useCallback(async () => {
    try {
      setGraph(await api.serviceGraph())
      setGraphError(null)
    } catch (error) {
      setGraphError(`Could not load dependency graph: ${(error as Error).message}`)
    }
  }, [])

  const applyIncident = useCallback((incident: Incident) => {
    setIncidents((current) => upsertIncident(current, incident))
  }, [])

  useEffect(() => {
    let socket: WebSocket | null = null
    let retryTimer: number | undefined
    let retryMs = 1000
    let stopped = false

    queueMicrotask(() => {
      if (stopped) return
      void reloadIncidents()
      void reloadServices()
      void reloadGraph()
    })

    const connect = () => {
      socket = new WebSocket(liveUrl())
      socket.onopen = () => {
        retryMs = 1000
        setConnection('live')
        void reloadIncidents()
        void reloadServices()
        void reloadGraph()
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
  }, [reloadIncidents, reloadServices, reloadGraph])

  return {
    services,
    graph,
    incidents,
    connection,
    loadError: incidentError ?? serviceError ?? graphError,
    applyIncident,
    reloadServices,
  }
}
