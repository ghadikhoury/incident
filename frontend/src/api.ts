// Types mirror backend/incident_api/models.py and health.py.

export type Severity = 'SEV-1' | 'SEV-2' | 'SEV-3' | 'SEV-4'
export type Status = 'OPEN' | 'ACKNOWLEDGED' | 'INVESTIGATING' | 'MITIGATING' | 'RESOLVED'
export type HealthStatus = 'healthy' | 'unhealthy' | 'down' | 'unknown'
export type FailureType = 'db_slow' | 'latency' | 'error_rate' | 'crash' | 'intermittent' | 'cpu'

export const SEVERITIES: Severity[] = ['SEV-1', 'SEV-2', 'SEV-3', 'SEV-4']

export interface Incident {
  incident_id: string
  title: string
  service: string
  severity: Severity
  status: Status
  trigger: string
  summary: string | null
  assigned_to: string | null
  created_at: string
  updated_at: string
  resolved_at: string | null
  alerts?: Alert[]
  probable_root?: string | null
  downstream_services?: string[]
  correlation_label?: string | null
  severity_reason?: string | null
  diagnosis?: Diagnosis | null
}

export interface TimelineEvent {
  at: string
  kind: string
  message: string
  actor: string | null
}

export interface IncidentDetail extends Incident {
  timeline: TimelineEvent[]
}

export interface MetricPoint { at: string; value: number }
export interface MetricSeries { service: string; metric: 'Latency' | 'Requests' | 'Errors' | 'HealthLatency' | 'HealthCheckFailed'; points: MetricPoint[] }
export interface IncidentMetrics { start: string; end: string; series: MetricSeries[] }
export interface IncidentLogs { source: string; rows: Record<string, string>[] }

export interface Recommendation {
  id: string
  action: 'clear_chaos'
  service: string
  reason: string
  status: 'PENDING' | 'APPROVED' | 'EXECUTING' | 'REJECTED' | 'SUCCEEDED' | 'FAILED'
  decided_by: string | null
  changed_at: string | null
}

export interface Diagnosis {
  status: 'RUNNING' | 'READY' | 'UNAVAILABLE'
  claimed_at: string
  unavailable_reason?: 'missing_configuration' | 'provider_failure' | null
  summary: string | null
  likely_root_cause: string | null
  confidence: 'low' | 'medium' | 'high' | null
  evidence: string[]
  recommended_actions: Recommendation[]
}

export interface Alert {
  alarm_name: string
  service: string
  signal: 'health' | 'errors' | 'latency'
  state: 'ALARM' | 'OK' | 'INSUFFICIENT_DATA'
  first_at: string
  last_at: string
  observed_value: number | null
  threshold: number | null
}

export interface ServiceNode {
  name: string
  display_name: string
  depends_on: string[]
  monitored: boolean
}

export interface Chaos {
  latency_ms: number
  error_rate: number
  db_delay_s: number
  intermittent_on_s: number
  intermittent_off_s: number
  cpu_ms: number
}

export interface ServiceHealth {
  name: string
  display_name: string
  status: HealthStatus
  depends_on: string[]
  error: string | null
  response_ms: number | null
  chaos: Chaos | null
  checked_at: string | null
}

export type LiveMessage =
  | { type: 'services'; data: ServiceHealth[] }
  | { type: 'incident'; data: Incident }

export interface NewIncident {
  title: string
  service: string
  severity: Severity
  summary?: string
  actor?: string
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const payload = await response.json()
      if (typeof payload.detail === 'string') detail = payload.detail
    } catch {
      // not JSON; keep the status text
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

const actorBody = (actor: string) => ({ actor: actor || undefined })

export interface Environment {
  mode: 'local' | 'aws'
  incident_source: string
  detection: { source: string; status: 'available' | 'unavailable' | 'configured' | 'queue_not_configured'; checked_at: string | null; failure_duration_s: number | null }
  diagnosis: { provider: string; configuration: string }
  restart_commands: Record<string, string>
}

export const api = {
  environment: () => request<Environment>('GET', '/api/environment'),
  services: () => request<ServiceHealth[]>('GET', '/api/services'),
  serviceGraph: () => request<ServiceNode[]>('GET', '/api/services/graph'),
  incidents: () => request<Incident[]>('GET', '/api/incidents'),
  incident: (id: string) => request<IncidentDetail>('GET', `/api/incidents/${encodeURIComponent(id)}`),
  incidentMetrics: (id: string) => request<IncidentMetrics>('GET', `/api/incidents/${encodeURIComponent(id)}/metrics`),
  incidentLogs: (id: string) => request<IncidentLogs>('GET', `/api/incidents/${encodeURIComponent(id)}/logs`),
  searchIncidentLogs: (id: string, query: string) => request<IncidentLogs>(
    'GET', `/api/incidents/${encodeURIComponent(id)}/log-search?q=${encodeURIComponent(query)}`,
  ),
  updateIncident: (id: string, changes: { assigned_to?: string | null; severity?: Severity }, actor: string) =>
    request<Incident>('PATCH', `/api/incidents/${encodeURIComponent(id)}`, { ...changes, actor }),
  createIncident: (incident: NewIncident) => request<Incident>('POST', '/api/incidents', incident),
  acknowledge: (id: string, actor: string) =>
    request<Incident>('POST', `/api/incidents/${id}/acknowledge`, actorBody(actor)),
  resolve: (id: string, actor: string) =>
    request<Incident>('POST', `/api/incidents/${id}/resolve`, actorBody(actor)),
  injectFailure: (service: string, failure: FailureType) =>
    request<unknown>('POST', '/api/simulation/failure', { service, failure }),
  recover: (service: string) => request<unknown>('POST', '/api/simulation/recover', { service }),
  decideRecommendation: (id: string, actionId: string, decision: 'approve' | 'reject', actor: string) =>
    request<Incident>(
      'POST',
      `/api/incidents/${encodeURIComponent(id)}/recommendations/${encodeURIComponent(actionId)}/${decision}`,
      { actor },
    ),
  retryDiagnosis: (id: string, actor: string) =>
    request<Incident>('POST', `/api/incidents/${encodeURIComponent(id)}/diagnosis/retry`, { actor }),
}

export function liveUrl(location: Location = window.location): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${location.host}/api/ws`
}
