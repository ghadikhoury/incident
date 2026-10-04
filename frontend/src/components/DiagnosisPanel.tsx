import type { Incident } from '../api'

interface Props {
  incident: Incident | undefined
  actor: string
  busy: boolean
  onApprove: (id: string, actionId: string) => void
  onReject: (id: string, actionId: string) => void
  onRetry: (id: string) => void
}

export function DiagnosisPanel({ incident, actor, busy, onApprove, onReject, onRetry }: Props) {
  if (!incident) return null
  const diagnosis = incident.diagnosis
  return (
    <section className="panel diagnosis-panel" aria-label="Incident analysis">
      <div className="panel__header">
        <h2>Incident analysis · {incident.incident_id}</h2>
        <span className="muted">{incident.status}</span>
      </div>
      <div className="analysis-columns">
        <div>
          <h3>Observed facts</h3>
          <p>{incident.alerts?.length ?? 0} recorded alarms for {incident.service}.</p>
          {incident.alerts?.length ? (
            <ul>
              {incident.alerts.map((alert) => (
                <li key={alert.alarm_name}>
                  {alert.service} {alert.signal}: {alert.state}
                  {alert.observed_value != null && ` · observed ${alert.observed_value}`}
                  {alert.threshold != null && ` · threshold ${alert.threshold}`}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">No collected observations attached.</p>
          )}
        </div>
        <div>
          <h3>AI inference</h3>
          {diagnosis && <p className="muted">AI status: {diagnosis.status} · Evidence snapshot analyzed at {diagnosis.claimed_at}. Later observations appear in the collected evidence and timeline.</p>}
          {!diagnosis && <p className="muted">
            {incident.trigger === 'CLOUDWATCH' || incident.trigger === 'LOCAL_HEALTH'
              ? 'Collecting observed evidence before analysis.'
              : 'No automatic evidence available for this incident.'}
          </p>}
          {diagnosis?.status === 'RUNNING' && <p>Analyzing saved evidence…</p>}
          {diagnosis?.status === 'UNAVAILABLE' && (
            <>
              <p>AI analysis unavailable.</p>
              <p className="muted">{diagnosis.unavailable_reason === 'missing_configuration'
                ? 'The backend Gemini API key is missing. Detection and collected evidence remain available.'
                : 'The model call or evidence validation failed. No recommended actions were produced.'}</p>
              {incident.status !== 'RESOLVED' && (
                <button disabled={busy || !actor.trim()} onClick={() => onRetry(incident.incident_id)}>
                  Retry analysis
                </button>
              )}
            </>
          )}
          {diagnosis?.status === 'READY' && (
            <>
              <p>{diagnosis.summary}</p>
              <p><strong>Likely root cause:</strong> {diagnosis.likely_root_cause}</p>
              <p className="muted">Confidence: {diagnosis.confidence}</p>
              <h4>Supporting evidence</h4>
              <ul>{diagnosis.evidence.map((line, index) => <li key={index}>{line}</li>)}</ul>
              <h4>Recommended actions</h4>
              {diagnosis.recommended_actions.length === 0 && <p className="muted">No safe action recommended.</p>}
              {diagnosis.recommended_actions.map((action) => (
                <div className="recommendation" key={action.id}>
                  <strong>Reset simulated fault on {action.service}</strong>
                  <p>{action.reason}</p>
                  <p className="muted">Decision: {action.status}{action.decided_by && ` by ${action.decided_by}`}</p>
                  {incident.status !== 'RESOLVED' && (action.status === 'PENDING' || action.status === 'FAILED') && (
                    <div className="recommendation__buttons">
                      <button disabled={busy || !actor.trim()} onClick={() => onApprove(incident.incident_id, action.id)}>
                        {action.status === 'FAILED' ? 'Retry approved action' : 'Approve and run'}
                      </button>
                      {action.status === 'PENDING' && (
                        <button disabled={busy || !actor.trim()} onClick={() => onReject(incident.incident_id, action.id)}>
                          Reject
                        </button>
                      )}
                    </div>
                  )}
                </div>
              ))}
              {!actor.trim() && diagnosis.recommended_actions.some((action) => action.status === 'PENDING') && (
                <p className="muted">Enter your name above to record a decision.</p>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  )
}
