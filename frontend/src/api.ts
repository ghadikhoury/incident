// Types mirror backend/incident_api/models.py and health.py.

export type Severity = 'SEV-1' | 'SEV-2' | 'SEV-3' | 'SEV-4'
export type Status = 'OPEN' | 'ACKNOWLEDGED' | 'INVESTIGATING' | 'MITIGATING' | 'RESOLVED'
export type HealthStatus = 'healthy' | 'unhealthy' | 'down' | 'unknown'
export type FailureType = 'db_slow' | 'latency' | 'error_rate' | 'crash'

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

export const api = {
  services: () => request<ServiceHealth[]>('GET', '/api/services'),
  serviceGraph: () => request<ServiceNode[]>('GET', '/api/services/graph'),
  incidents: () => request<Incident[]>('GET', '/api/incidents'),
  createIncident: (incident: NewIncident) => request<Incident>('POST', '/api/incidents', incident),
  acknowledge: (id: string, actor: string) =>
    request<Incident>('POST', `/api/incidents/${id}/acknowledge`, actorBody(actor)),
  resolve: (id: string, actor: string) =>
    request<Incident>('POST', `/api/incidents/${id}/resolve`, actorBody(actor)),
  injectFailure: (service: string, failure: FailureType) =>
    request<unknown>('POST', '/api/simulation/failure', { service, failure }),
  recover: (service: string) => request<unknown>('POST', '/api/simulation/recover', { service }),
}

export function liveUrl(location: Location = window.location): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${location.host}/api/ws`
}
