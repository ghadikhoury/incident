import { useState, type FormEvent } from 'react'
import { SEVERITIES, type FailureType, type NewIncident, type ServiceHealth, type Severity } from '../api'

const FAILURES: { value: FailureType; label: string }[] = [
  { value: 'db_slow', label: 'Slow database (payment only)' },
  { value: 'latency', label: 'Latency +3 s' },
  { value: 'error_rate', label: '40% errors' },
  { value: 'crash', label: 'Crash' },
  { value: 'intermittent', label: 'Every other request fails' },
  { value: 'cpu', label: 'CPU pressure' },
]

interface Props {
  services: ServiceHealth[]
  busy: boolean
  onInject: (service: string, failure: FailureType) => void
  onRecover: (service: string) => void
  onDeclare: (incident: NewIncident) => Promise<boolean>
}

export function DemoPanel({ services, busy, onInject, onRecover, onDeclare }: Props) {
  const names = services.map((s) => s.name)
  const [target, setTarget] = useState('payment')
  const [failure, setFailure] = useState<FailureType>('db_slow')

  const [title, setTitle] = useState('')
  const [service, setService] = useState('payment')
  const [severity, setSeverity] = useState<Severity>('SEV-3')

  const declare = async (event: FormEvent) => {
    event.preventDefault()
    if (await onDeclare({ title: title.trim(), service, severity })) setTitle('')
  }

  return (
    <aside className="panel demo">
      <h2>Simulation</h2>
      <p className="muted">Break the simulated shop on purpose and watch Incident react.</p>
      <div className="field-row">
        <select value={target} onChange={(e) => setTarget(e.target.value)} aria-label="Service">
          {names.map((name) => (
            <option key={name}>{name}</option>
          ))}
        </select>
        <select
          value={failure}
          onChange={(e) => setFailure(e.target.value as FailureType)}
          aria-label="Failure"
        >
          {FAILURES.map((f) => (
            <option key={f.value} value={f.value}>
              {f.label}
            </option>
          ))}
        </select>
      </div>
      <div className="field-row">
        <button className="danger" disabled={busy} onClick={() => onInject(target, failure)}>
          Inject failure
        </button>
        <button disabled={busy} onClick={() => onRecover(target)}>
          Recover
        </button>
      </div>

      <h2>Declare incident</h2>
      <form onSubmit={declare} className="declare">
        <input
          placeholder="What's wrong?"
          value={title}
          maxLength={200}
          onChange={(e) => setTitle(e.target.value)}
          aria-label="Title"
        />
        <div className="field-row">
          <select value={service} onChange={(e) => setService(e.target.value)} aria-label="Service">
            {names.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
          <select
            value={severity}
            onChange={(e) => setSeverity(e.target.value as Severity)}
            aria-label="Severity"
          >
            {SEVERITIES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </div>
        <button type="submit" disabled={busy || title.trim() === ''}>
          Declare
        </button>
      </form>
    </aside>
  )
}
