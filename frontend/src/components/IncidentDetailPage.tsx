import { useEffect, useState } from 'react'
import {
  api,
  SEVERITIES,
  type Incident,
  type Environment,
  type IncidentDetail,
  type IncidentLogs,
  type IncidentMetrics,
  type MetricSeries,
  type ServiceHealth,
  type ServiceNode,
  type Severity,
} from '../api'
import { DependencyGraph } from './DependencyGraph'
import { DiagnosisPanel } from './DiagnosisPanel'
import { RecoveryInstructions } from './RecoveryInstructions'

const COLORS = ['#58a6ff', '#3fb950', '#d29922', '#e879f9']
const METRIC_LABELS = { Latency: 'Latency p90 (ms)', Requests: 'Requests / min', Errors: 'Server errors / min', HealthLatency: 'Health-probe latency (ms)', HealthCheckFailed: 'Health-probe failure (1 = failed)' }

function MetricChart({ metric, series, start, end }: {
  metric: MetricSeries['metric']; series: MetricSeries[]; start: string; end: string
}) {
  const shown = series.filter((item) => item.metric === metric && item.points.length)
  const from = Date.parse(start)
  const span = Math.max(1, Date.parse(end) - from)
  const maximum = Math.max(1, ...shown.flatMap((item) => item.points.map((point) => point.value)))
  return (
    <div className="metric-chart">
      <h3>{METRIC_LABELS[metric]}</h3>
      {shown.length === 0 ? <p className="muted">No collected datapoints in this window.</p> : (
        <>
          <svg viewBox="0 0 600 160" role="img" aria-label={`${METRIC_LABELS[metric]} over time`}>
            <line x1="45" y1="15" x2="45" y2="125" className="chart-axis" />
            <line x1="45" y1="125" x2="585" y2="125" className="chart-axis" />
            <text x="2" y="20" className="chart-label">{maximum.toFixed(metric === 'Latency' ? 0 : 1)}</text>
            <text x="35" y="145" className="chart-label">{new Date(start).toLocaleTimeString()}</text>
            <text x="475" y="145" className="chart-label">{new Date(end).toLocaleTimeString()}</text>
            {shown.map((item, index) => item.points.length === 1 ? (
              <circle
                key={item.service}
                cx={45 + 540 * (Date.parse(item.points[0].at) - from) / span}
                cy={125 - 105 * item.points[0].value / maximum}
                r="3"
                fill={COLORS[index % COLORS.length]}
              />
            ) : (
              <polyline
                key={item.service}
                fill="none"
                stroke={COLORS[index % COLORS.length]}
                strokeWidth="2.5"
                points={item.points.map((point) =>
                  `${45 + 540 * (Date.parse(point.at) - from) / span},${125 - 105 * point.value / maximum}`
                ).join(' ')}
              />
            ))}
          </svg>
          <div className="chart-legend">
            {shown.map((item, index) => <span key={item.service} style={{ color: COLORS[index % COLORS.length] }}>{item.service}</span>)}
          </div>
        </>
      )}
    </div>
  )
}

interface Props {
  incident: Incident
  graph: ServiceNode[]
  services: ServiceHealth[]
  environment?: Environment | null
  actor: string
  busy: boolean
  onBack: () => void
  onAcknowledge: (id: string) => void
  onResolve: (id: string) => void
  onPatch: (id: string, changes: { assigned_to?: string | null; severity?: Severity }) => void
  onApprove: (id: string, actionId: string) => void
  onReject: (id: string, actionId: string) => void
  onRetry: (id: string) => void
}

export function IncidentDetailPage({ incident, graph, services, environment, actor, busy, onBack, onAcknowledge,
  onResolve, onPatch, onApprove, onReject, onRetry }: Props) {
  const [detail, setDetail] = useState<IncidentDetail | null>(null)
  const [metrics, setMetrics] = useState<IncidentMetrics | null>(null)
  const [logs, setLogs] = useState<IncidentLogs | null>(null)
  const [searchResults, setSearchResults] = useState<IncidentLogs | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [telemetryError, setTelemetryError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [ownerDraft, setOwnerDraft] = useState<string | null>(null)
  const local = environment?.mode === 'local' || incident.trigger === 'LOCAL_HEALTH'
  const restart = local && services.find((s) => s.name === (incident.probable_root ?? incident.service))?.status === 'down'
    ? environment?.restart_commands[incident.probable_root ?? incident.service] : null

  useEffect(() => {
    let live = true
    const load = async () => {
      try {
        const result = await api.incident(incident.incident_id)
        if (live) { setDetail(result); setError(null) }
      } catch (cause) {
        if (live) setError(`Timeline unavailable: ${(cause as Error).message}`)
      }
    }
    void load()
    const timer = window.setInterval(() => { void load() }, 30_000)
    return () => { live = false; window.clearInterval(timer) }
  }, [incident.incident_id, incident.updated_at])

  useEffect(() => {
    let live = true
    const load = async () => {
      const [metricResult, logResult] = await Promise.allSettled([
        api.incidentMetrics(incident.incident_id), api.incidentLogs(incident.incident_id),
      ])
      if (!live) return
      if (metricResult.status === 'fulfilled') setMetrics(metricResult.value)
      if (logResult.status === 'fulfilled') setLogs(logResult.value)
      setTelemetryError([metricResult, logResult].filter((result) => result.status === 'rejected')
        .map((result) => (result as PromiseRejectedResult).reason.message).join('; ') || null)
    }
    void load()
    const timer = window.setInterval(() => { void load() }, 30_000)
    return () => { live = false; window.clearInterval(timer) }
  }, [incident.incident_id, incident.updated_at])

  const current = detail && detail.updated_at >= incident.updated_at ? detail : incident
  const owner = ownerDraft ?? current.assigned_to ?? ''
  const matchingLogs = (searchResults?.rows ?? logs?.rows ?? []).filter((row) =>
    Object.values(row).some((value) => value.toLowerCase().includes(query.trim().toLowerCase()))
  )
  const visibleLogs = matchingLogs.length > 200
    ? [...matchingLogs.slice(0, 100), ...matchingLogs.slice(-100)] : matchingLogs
  const searchCloudWatch = async () => {
    if (!query.trim()) return
    setSearching(true)
    setSearchError(null)
    try {
      setSearchResults(await api.searchIncidentLogs(current.incident_id, query.trim()))
    } catch (cause) {
      setSearchError(`Evidence search failed: ${(cause as Error).message}`)
    } finally {
      setSearching(false)
    }
  }

  return (
    <main className="detail-page">
      <button onClick={onBack}>← All incidents</button>
      <section className="panel detail-heading">
        <div>
          <p className="muted mono">{current.incident_id} · {current.created_at}</p>
          <h2>{current.title}</h2>
          <p>{current.summary}</p>
          <p>Probable root: <strong>{current.probable_root ?? current.service}</strong>
            {current.downstream_services?.length ? ` · Downstream: ${current.downstream_services.join(', ')}` : ''}</p>
          <p className="muted">{current.severity_reason}</p>
        </div>
        <div className="detail-controls">
          <span className={`status status--${current.status}`}>{current.status}</span>
          <label>Severity
            <select aria-label="Incident severity" value={current.severity} disabled={busy || current.status === 'RESOLVED' || !actor.trim()}
              onChange={(event) => onPatch(current.incident_id, { severity: event.target.value as Severity })}>
              {SEVERITIES.map((level) => <option key={level}>{level}</option>)}
            </select>
          </label>
          <label>Owner
            <input aria-label="Incident owner" value={owner} maxLength={64} disabled={busy || current.status === 'RESOLVED'}
              onChange={(event) => setOwnerDraft(event.target.value)} />
          </label>
          <button disabled={busy || !actor.trim() || current.status === 'RESOLVED' || owner.trim() === (current.assigned_to ?? '')}
            onClick={() => onPatch(current.incident_id, { assigned_to: owner.trim() || null })}>Save owner</button>
          {current.status === 'OPEN' && <button disabled={busy || !actor.trim()} onClick={() => onAcknowledge(current.incident_id)}>Acknowledge</button>}
          {current.status !== 'RESOLVED' && <button disabled={busy || !actor.trim()} onClick={() => onResolve(current.incident_id)}>Resolve</button>}
        </div>
      </section>

      <section className="panel" aria-label="Metric graphs">
        <h2>{local ? 'Collected health metrics' : 'CloudWatch metrics'}</h2>
        {telemetryError && <p role="alert">{telemetryError}</p>}
        {!metrics && <p className="muted">Loading metric history…</p>}
        {metrics && <div className="metric-grid">
          {(local ? ['HealthLatency', 'HealthCheckFailed'] as const : ['Latency', 'Requests', 'Errors'] as const).map((metric) =>
            <MetricChart key={metric} metric={metric} series={metrics.series} start={metrics.start} end={metrics.end} />
          )}
        </div>}
        <p className="muted">{local ? 'Actual /health probes. Probe latency is not request latency. Evidence retains initial observations and the latest probes, capped at 800 samples per incident.' : 'One-minute CloudWatch datapoints, from five minutes before the incident. The view is capped at 30 minutes.'}</p>
      </section>

      <section className="panel" aria-label="Saved logs">
        <h2>{local ? 'Collected health evidence' : 'Saved log excerpts'}</h2>
        <p className="muted">{local ? 'Observed probe results and failures, not container logs or injection settings. Recovery adds healthy observations and OK alerts; resolving the incident is a separate engineer decision.' : 'Saved excerpts load automatically. Enter a term and search the full CloudWatch incident window when you need more than the saved samples.'}</p>
        <div className="log-search">
          <input aria-label="Search saved logs" placeholder="Search messages, errors, trace IDs…" value={query}
            maxLength={100} onChange={(event) => { setQuery(event.target.value); setSearchResults(null) }} />
          <button disabled={!query.trim() || searching} onClick={() => { void searchCloudWatch() }}>
            {searching ? 'Searching…' : local ? 'Search collected evidence' : 'Search CloudWatch'}
          </button>
        </div>
        {searchError && <p role="alert">{searchError}</p>}
        {!logs && <p className="muted">Loading saved log excerpts…</p>}
        {(logs || searchResults) && <p className="muted">{searchResults?.source ?? logs?.source}: {matchingLogs.length} matching lines{matchingLogs.length > 200 ? ' · showing initial 100 and latest 100' : ''}</p>}
        <div className="log-list">
          {visibleLogs.map((row, index) => <div className="log-row" key={`${row['@timestamp']}-${row.trace_id}-${index}`}>
            <span className="mono muted">{row['@timestamp']} · {row.service}</span>
            <span>{row.error_type ?? row.message ?? row.error_message ?? JSON.stringify(row)}</span>
            {row.error_message && <span className="muted">{row.error_message}</span>}
          </div>)}
        </div>
      </section>

      <div className="detail-grid">
        <section className="panel" aria-label="Related alerts">
          <h2>Related alerts</h2>
          {!current.alerts?.length && <p className="muted">No observed alerts attached.</p>}
          <ul>{current.alerts?.map((alert) => <li key={alert.alarm_name}>
            <strong>{alert.service} {alert.signal}</strong> · {alert.state} · first {alert.first_at}
            {alert.observed_value != null && ` · observed ${alert.observed_value}`}
            {alert.threshold != null && ` / threshold ${alert.threshold}`}
          </li>)}</ul>
        </section>
        <DependencyGraph nodes={graph} services={services}
          alertedServices={new Set(current.alerts?.filter((alert) => alert.state === 'ALARM').map((alert) => alert.service))} />
      </div>

      {restart && <section className="panel">
        <RecoveryInstructions command={restart} />
      </section>}
      <DiagnosisPanel incident={current} actor={actor} busy={busy} onApprove={onApprove} onReject={onReject} onRetry={onRetry} />

      <section className="panel" aria-label="Incident timeline">
        <h2>Full timeline</h2>
        {error && <p role="alert">{error}</p>}
        {!detail && !error && <p className="muted">Loading timeline…</p>}
        <ol className="timeline">{detail?.timeline.map((event, index) => <li key={`${event.at}-${index}`}>
          <time dateTime={event.at} className="mono muted">{new Date(event.at).toLocaleString()}</time>
          <strong>{event.kind}</strong> {event.message} {event.actor && <span className="muted">by {event.actor}</span>}
        </li>)}</ol>
      </section>
    </main>
  )
}
