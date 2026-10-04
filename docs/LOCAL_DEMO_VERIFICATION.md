# Local demo verification — 2026-10-04

Backend: `feat/local-incident-detection` (PR #16). Dashboard changes are stacked
on that branch. Both PRs are intended for independent review and remain unmerged.

## Automated checks

- Full Python suite: **160 passed**, 69.60 seconds. One pre-existing Starlette
  TestClient deprecation warning. Fake services/providers and moto; no live key.
- Local regressions: **16 passed**, 5.48 seconds. Persistent and transient
  failures, elapsed time, duplicate probes, long gaps, restart persistence,
  correlation/deduplication, recovery vs resolution, redaction and bounded
  evidence, local/AWS isolation, missing/failing model configuration, explicit
  retry to READY, and human approval plus recovery verification.
- Frontend suite: **28 passed**, using fake API responses. Covers local evidence
  and metric labels, latest recovery visibility, mode/isolation banner, missing
  configuration, explicit retry, operator restart instructions, and discarding
  cached AWS incidents when a backend mode switch is observed.
- Ruff lint and formatting (72 Python files), frontend lint, production build,
  and `git diff --check`: passed.
- Windows Vitest fork workers initially failed to start. The successful local
  run used `npm test -- --pool=threads --maxWorkers=1`; CI retains its normal
  Linux runner and test command.
- Backend and frontend were rebuilt with `docker compose up -d --build backend
  frontend`. Private runtime key scan passed; `.env` is ignored and untracked.

## Real local browser scenario

1. Backend startup found the already-stopped inventory container and created
   local `INC-1001` automatically. Its real Gemini diagnosis reached `READY`.
2. Started inventory with the displayed operator command. Verified healthy
   probes and an `OK` alert while the incident remained `OPEN`. Explicitly
   resolved this setup incident through the dashboard under
   `local-demo-verification`, establishing an empty active-incident baseline.
3. Browser selected **inventory → Crash → Inject failure** at
   **2026-10-04T21:11:34.269Z**. Inventory became Down; no incident appeared
   immediately. No manual declaration or direct incident creation was used.
4. The detector created **INC-1002** at
   **2026-10-04T21:11:48.974421Z**, **14.705421 seconds** after the click.
   First failing probe: **21:11:34.402333Z**. Saved observations include actual
   ReadError/ConnectTimeout failures and healthy probes from other services.
5. **Review** displayed collected health graphs, probe evidence, related alerts,
   timeline, and a real **Gemini READY** diagnosis. Diagnosis claimed at
   **21:11:54.995845Z**; its citations identify saved probe timestamps and errors.
   It described service unreachability, rather than being told the injected fault.
6. UI showed **docker compose start inventory** because a crashed container
   cannot answer `/chaos`. Executed that explicit local operator command.
   Healthy probes verified recovery; the alert became **OK** at
   **21:14:58.822845Z**. **INC-1002 remains OPEN**, with a timestamped READY
   diagnosis and a **PENDING** recommendation. No recommendation was approved
   or executed. The dashboard remains open on its Review page.

Two real Gemini diagnosis requests succeeded during this walkthrough, both
using the previously privately configured **gemini-3.5-flash-lite** model and
synthetic local probe evidence. These are separate from the fake-based tests.
No live Bedrock request was made; no claim of live Bedrock availability is made.
No AWS deployment, infrastructure, IAM, credentials, or incidents were changed.

## Limitations and remaining steps

Local detection currently observes `/health`, not request errors when that
endpoint remains healthy. Dependency correlation is regression-tested with
observed fake downstream failures; this real inventory crash produced an
inventory health alarm only. Evidence identifies unreachability but cannot prove
the process-exit mechanism without container logs. Diagnoses are snapshots, not
automatic rewrites after recovery. Run one local backend instance.

Review both focused PRs, then merge the backend first and retarget the stacked
dashboard PR to main. Rebuild the local Compose stack after merging. No AWS
deployment is required for the local flow. The private key remains runtime-only;
model configuration alone does not guarantee free-tier quota or availability.
