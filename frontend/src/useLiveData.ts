import { useCallback, useEffect, useRef, useState } from 'react'
import { api, liveUrl, type Environment, type Incident, type LiveMessage, type ServiceHealth, type ServiceNode } from './api'
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
  const [environment, setEnvironment] = useState<Environment | null>(null)
  const [environmentError, setEnvironmentError] = useState<string | null>(null)
  const modeRef = useRef<Environment['mode'] | null>(null)
  const generation = useRef(0)
  const environmentRequest = useRef(0)
  // Capture before starting a request; checking only at completion loses its origin.
  const captureEnvironment = useCallback(() => {
    const started = generation.current
    return () => started === generation.current
  }, [])
  const reloadEnvironment = useCallback(async () => {
    const requestId = ++environmentRequest.current
    try {
      const fetched = await api.environment()
      if (requestId !== environmentRequest.current) return false
      const changed = modeRef.current !== null && modeRef.current !== fetched.mode
      if (changed) {
        generation.current++
        setIncidents([])
        setServices([])
        setGraph([])
        setIncidentError(null)
        setServiceError(null)
        setGraphError(null)
        window.location.hash = ''
      }
      modeRef.current = fetched.mode
      setEnvironment(fetched)
      setEnvironmentError(null)
      return changed
    } catch {
      if (requestId === environmentRequest.current)
        setEnvironmentError('Environment and detection availability could not be verified.')
      return false
    }
  }, [])

  const reloadIncidents = useCallback(async () => {
    await reloadEnvironment()
    const isCurrent = captureEnvironment()
    try {
      const fetched = await api.incidents()
      if (!isCurrent()) return
      setIncidents((current) => isCurrent() ? mergeIncidents(current, fetched) : current)
      setIncidentError(null)
    } catch (error) {
      if (isCurrent()) setIncidentError(`Could not load incidents: ${(error as Error).message}`)
    }
  }, [reloadEnvironment, captureEnvironment])

  const reloadServices = useCallback(async () => {
    const isCurrent = captureEnvironment()
    try {
      const fetched = await api.services()
      if (!isCurrent()) return
      setServices(fetched)
      setServiceError(null)
    } catch (error) {
      if (isCurrent()) setServiceError(`Could not load services: ${(error as Error).message}`)
    }
  }, [captureEnvironment])

  const reloadGraph = useCallback(async () => {
    const isCurrent = captureEnvironment()
    try {
      const fetched = await api.serviceGraph()
      if (!isCurrent()) return
      setGraph(fetched)
      setGraphError(null)
    } catch (error) {
      if (isCurrent()) setGraphError(`Could not load dependency graph: ${(error as Error).message}`)
    }
  }, [captureEnvironment])

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
      if (stopped) return
      const openedSocket = new WebSocket(liveUrl())
      socket = openedSocket
      const isCurrent = captureEnvironment()
      let closed = false
      const ownsConnection = () => !stopped && !closed && socket === openedSocket && isCurrent()
      setConnection('connecting')
      openedSocket.onopen = () => {
        if (!ownsConnection()) return
        retryMs = 1000
        setConnection('live')
        void reloadIncidents()
        void reloadServices()
        void reloadGraph()
      }
      openedSocket.onmessage = (event) => {
        if (!ownsConnection()) return
        const message = JSON.parse(event.data) as LiveMessage
        if (message.type === 'services') setServices(message.data)
        else if (message.type === 'incident')
          setIncidents((current) => ownsConnection() ? upsertIncident(current, message.data) : current)
      }
      openedSocket.onclose = () => {
        if (!ownsConnection()) return
        closed = true
        setConnection('offline')
        retryTimer = window.setTimeout(connect, retryMs)
        retryMs = Math.min(retryMs * 2, MAX_RETRY_MS)
      }
    }

    connect()
    const environmentTimer = window.setInterval(() => {
      void reloadEnvironment().then((changed) => {
        if (!stopped && changed) { void reloadIncidents(); void reloadServices(); void reloadGraph() }
      })
    }, 5000)
    return () => {
      stopped = true
      window.clearTimeout(retryTimer)
      socket?.close()
      window.clearInterval(environmentTimer)
    }
  }, [reloadIncidents, reloadServices, reloadGraph, reloadEnvironment, captureEnvironment, environment?.mode])

  return {
    services,
    graph,
    environment,
    incidents,
    connection,
    loadError: incidentError ?? serviceError ?? graphError ?? environmentError,
    applyIncident,
    captureEnvironment,
    reloadServices,
  }
}
