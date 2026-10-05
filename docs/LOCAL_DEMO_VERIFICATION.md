# Local demo verification — 2026-10-04

Backend PR #16 was independently reviewed and merged into main at
`0d47905b6fcfef29245cf56c3026da05daa41a10`. Dashboard
[PR #17](https://github.com/ghadikhoury/incident/pull/17) was independently
rereviewed and merged into main at
`55e6f9f2147fe624ab4c3b37c12d09d13869cea3`. The results below record that
walkthrough and review; they are not a new accuracy evaluation.

## Recorded PR #17 automated checks

- Full Python suite: **160 passed**, 69.60 seconds. One pre-existing Starlette
  TestClient deprecation warning. Fake services/providers and moto; no live key.
- Local regressions: **16 passed**, 5.48 seconds. Persistent and transient
  failures, elapsed time, duplicate probes, long gaps, restart persistence,
  correlation/deduplication, recovery vs resolution, redaction and bounded
  evidence, local/AWS isolation, missing/failing model configuration, explicit
  retry to READY, and human approval plus recovery verification.
- Frontend suite after the review fixes: **39 passed**, 19.90 seconds, using fake API responses. Covers local evidence
  and metric labels, latest recovery visibility, mode/isolation banner, missing
  configuration, explicit retry, operator restart instructions, and discarding
  cached AWS incidents when a backend mode switch is observed, deferred action
  successes/errors with colliding incident IDs, deferred incident detail and
  incident/service/dependency fetches, and obsolete or closed WebSocket events.
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
using **gemini-3.5-flash-lite** (now the local example and default) and
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

PRs #16 and #17 are merged. Build the local Compose stack from main to use
the completed local flow; no AWS deployment is required. The private key remains
runtime-only;
model configuration alone does not guarantee free-tier quota or availability.
Two successful requests do not measure AI accuracy or production reliability.
Human approval is covered by fake-based tests; this real crash was recovered
with an operator command, without approving or executing a recommendation.

## PR #17 review regression

The reviewer reproduced an AWS acknowledgment response arriving after a switch
to local mode and reinserting an AWS record under the local banner. Before the
fix, three new App regressions failed: delayed action success, delayed action
error, and obsolete WebSocket messages. All now pass.

Every App action captures an environment generation before issuing its request.
Results, errors, and busy state are scoped to that generation. Deferred detail,
list, health, and dependency responses are guarded too. Mode changes clear
cached data, retire the old socket, and establish a new connection. Closed and
obsolete sockets cannot update incident, service, or connection state; current
responses and reconnects still work. These races were exercised with deferred
fake responses, without switching a real backend into AWS mode or using a model
key. No additional model call or remediation was performed for these fixes.

After `docker compose up -d --build --no-deps frontend`, browser verification at
`http://127.0.0.1:3000` confirmed the local SQLite banner and available health
detection. Active incidents showed one incident; its Review button reopened
`INC-1002`, with collected evidence, a saved READY diagnosis, an OK recovery
alert, OPEN incident status, and a PENDING recommendation. The engineer identity
was left blank and approval controls stayed disabled. This rechecks navigation
and rendering of the existing real scenario; it does not count as a new live
model call or a real AWS/local mode-switch test.

## Main-branch verification refresh ? 2026-10-04

This update starts from merged main `55e6f9f` and aligns the example, application,
and Compose model defaults with `gemini-3.5-flash-lite`. Bedrock selection,
legacy Compose fallback, and operator model overrides remain available.

- `python -m infra.build`: passed; built the Linux Lambda asset locally.
- `pytest -p no:cacheprovider -q`: **164 passed**, 124.04 seconds, including all
  **4 infrastructure tests**. One existing Starlette TestClient warning. Service
  and backend checks use fakes/moto; CDK checks assert synthesized templates.
- `pytest integration -p no:cacheprovider -q` with `COMPOSE_PROJECT_NAME=incident`:
  **10 passed**, 47.94 seconds, against the real running local simulation.
  The first run used the worktree project name and failed container restart
  commands (9 passed, 1 failed, 1 teardown error); services were restored and
  the corrected run passed. These tests exercise faults and recovery, not AI accuracy.
- Ruff lint and format checks: passed (73 Python files already formatted).
- Frontend lint, TypeScript/production build, and
  `npm test -- --pool=threads --maxWorkers=1`: passed; **39 tests**, 56.57 seconds.
- Configuration assertions: passed for the application/example/Compose model,
  empty example key, explicit Gemini and Bedrock selections, legacy Bedrock
  fallback, model override, and EC2 Compose configuration.
- Browser recheck of the running dashboard reopened `INC-1002` and confirmed
  the original timestamped READY diagnosis, collected probe failures, recovered
  OK alert, OPEN incident, and PENDING recommendation. This reads the existing
  result; it is not a new model evaluation or an approval/execution test.

Default pytest discovery now includes `infra/tests`, and the Python CI job
explicitly installs Node 24 for CDK/jsii before building the Lambda asset and
running the complete suite. No new video was recorded for this update; the
existing Stage 1 video covers the earlier AWS demonstration.
