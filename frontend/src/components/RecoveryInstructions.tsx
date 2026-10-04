import { useState } from 'react'

export function RecoveryInstructions({ command }: { command: string }) {
  const [copyStatus, setCopyStatus] = useState('')
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command)
      setCopyStatus('Copied.')
    } catch {
      setCopyStatus('Copy unavailable. Select the command above and copy it manually.')
    }
  }
  return <div aria-label="Crash recovery">
    <h3>Operator recovery required</h3>
    <p>The service is unreachable. Check its local container; if it exited, run from the repository:</p>
    <pre><code>{command}</code></pre>
    <button onClick={() => { void copy() }}>Copy restart command</button>
    {copyStatus && <p role="status">{copyStatus}</p>}
    <p>Watch for healthy probes and OK alerts. The incident remains active until an engineer resolves it. Approved fault resets cannot restart a crashed container.</p>
  </div>
}
