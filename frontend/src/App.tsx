import { useEffect, useState } from 'react'
import { api, type FailureType, type NewIncident } from './api'
import { DemoPanel } from './components/DemoPanel'
import { IncidentDetailPage } from './components/IncidentDetailPage'
import { DependencyGraph } from './components/DependencyGraph'
import { IncidentTable } from './components/IncidentTable'
import { ServiceList } from './components/ServiceList'
import { systemHealth } from './logic'
import { useLiveData, type Connection } from './useLiveData'
import { useNow } from './useNow'

const ACTOR_KEY = 'incident.actor'

const CONNECTION_LABEL: Record<Connection, string> = {
  live: 'Live',
  connecting: 'Connecting…',
  offline: 'Offline, retrying…',
}

function loadActor(): string {
  try {
    return localStorage.getItem(ACTOR_KEY) ?? ''
  } catch {
    return ''
  }
}

function saveActor(actor: string) {
  try {
    localStorage.setItem(ACTOR_KEY, actor)
  } catch {
    // storage unavailable (private mode); the name just won't be remembered
  }
}

function selectedFromHash(): string | null {
  const match = /^#\/incidents\/(INC-\d+)$/.exec(window.location.hash)
  return match?.[1] ?? null
}

export default function App() {
  const { services, graph, incidents, connection, loadError, applyIncident, reloadServices } = useLiveData()
  const now = useNow()
  const [actor, setActor] = useState(loadActor)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(selectedFromHash)
  const [selectedError, setSelectedError] = useState<string | null>(null)

  useEffect(() => {
    const sync = () => { setSelectedError(null); setSelectedId(selectedFromHash()) }
    window.addEventListener('hashchange', sync)
    return () => window.removeEventListener('hashchange', sync)
  }, [])

  useEffect(() => {
    if (!selectedId || selectedError || incidents.some((item) => item.incident_id === selectedId)) return
    let live = true
    void api.incident(selectedId).then((incident) => {
      if (live) applyIncident(incident)
    }).catch((error: Error) => {
      if (live) setSelectedError(`Could not load incident ${selectedId}: ${error.message}`)
    })
    return () => { live = false }
  }, [selectedId, selectedError, incidents, applyIncident])

  // Apply successful responses immediately, including when the live socket is offline.
  const run = async <T,>(
    action: () => Promise<T>,
    onSuccess?: (result: T) => void | Promise<void>,
  ): Promise<boolean> => {
    setBusy(true)
    setActionError(null)
    try {
      const result = await action()
      await onSuccess?.(result)
      return true
    } catch (error) {
      setActionError((error as Error).message)
      return false
    } finally {
      setBusy(false)
    }
  }

  const health = systemHealth(services)
  const error = actionError ?? loadError
  const selected = incidents.find((incident) => incident.incident_id === selectedId)

  return (
    <div className="app">
      <header className="topbar">
        <h1>Incident</h1>
        <span className={`system system--${health}`}>System: {health}</span>
        <span className={`connection connection--${connection}`}>
          {CONNECTION_LABEL[connection]}
        </span>
        <label className="actor">
          You are
          <input
            value={actor}
            placeholder="your name"
            maxLength={64}
            onChange={(e) => {
              setActor(e.target.value)
              saveActor(e.target.value)
            }}
          />
        </label>
      </header>

      {error && (
        <div className="error-banner" role="alert">
          {error}
          <button onClick={() => setActionError(null)} aria-label="Dismiss">
            ×
          </button>
        </div>
      )}

      {selectedId && !selected && selectedError && <div className="error-banner" role="alert">{selectedError}</div>}
      {selectedId && selected ? <IncidentDetailPage
        key={selected.incident_id}
        incident={selected}
        graph={graph}
        services={services}
        actor={actor}
        busy={busy}
        onBack={() => { window.location.hash = ''; setSelectedId(null) }}
        onAcknowledge={(id) => { void run(() => api.acknowledge(id, actor.trim()), applyIncident) }}
        onResolve={(id) => { void run(() => api.resolve(id, actor.trim()), applyIncident) }}
        onPatch={(id, changes) => { void run(() => api.updateIncident(id, changes, actor.trim()), applyIncident) }}
        onApprove={(id, actionId) => {
          void run(() => api.decideRecommendation(id, actionId, 'approve', actor.trim()), async (updated) => {
            applyIncident(updated)
            await reloadServices()
          })
        }}
        onReject={(id, actionId) => { void run(() => api.decideRecommendation(id, actionId, 'reject', actor.trim()), applyIncident) }}
        onRetry={(id) => { void run(() => api.retryDiagnosis(id, actor.trim()), applyIncident) }}
      /> : <main className="layout">
        <div className="main-column">
          <IncidentTable
            incidents={incidents}
            now={now}
            busy={busy}
            onAcknowledge={(id) => run(() => api.acknowledge(id, actor), applyIncident)}
            onResolve={(id) => run(() => api.resolve(id, actor), applyIncident)}
            onSelect={(id) => { setSelectedError(null); window.location.hash = `/incidents/${id}`; setSelectedId(id) }}
          />
          <DependencyGraph nodes={graph} services={services} />
          <ServiceList services={services} />
        </div>
        <DemoPanel
          services={services}
          busy={busy}
          onInject={(service, failure: FailureType) =>
            run(() => api.injectFailure(service, failure), reloadServices)
          }
          onRecover={(service) => run(() => api.recover(service), reloadServices)}
          onDeclare={(incident: NewIncident) =>
            run(() => api.createIncident({ ...incident, actor: actor || undefined }), applyIncident)
          }
        />
      </main>}
    </div>
  )
}
