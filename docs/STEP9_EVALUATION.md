# Step 9 live evaluation

Run on 2026-10-03 against the EC2 demo in us-east-2. Each selected case
started with no active incident and healthy services, injected one labeled
failure, and polled the backend every five seconds for a new CloudWatch incident.
The runner waited for diagnosis and the alarm cascade, restored the service,
required every attached alarm to return to OK, then resolved the test incident.
It stopped on any failed recovery gate. These are four isolated demo runs, not a production reliability
estimate; the polling interval adds up to five seconds to detection time. The
machine-readable rows are in [step9-results.json](step9-results.json).

| Failure | Incident | Detection (s) | Alarms in incident | Probable root | Root correct | Recovered | AI status |
| --- | --- | ---: | ---: | --- | --- | --- | --- |
| Slow payment database | INC-1012 | 85.2 | 7 | payment | yes | yes | unavailable |
| Intermittent inventory errors, two minutes on / one minute off | INC-1015 | 140.3 | 3 | inventory | yes | yes | unavailable |
| Inventory CPU pressure | INC-1018 | 120.3 | 5 | inventory | yes | yes | unavailable |
| Payment process crash | INC-1017 | 150.4 | 3 | payment | yes | yes | unavailable |

The root service was correct in **4/4** selected incidents. Median detection was
**130.3 seconds**. Every case grouped its alarms into one incident and passed the
recovery gate. These measurements used the same detection and correlation code;
the slow-database run preceded later UI and CPU-recovery-only changes. The final
CPU run used commit `ac7bf99`.

Earlier development runs are not counted in that four-case summary:

| Failure and revision | Incident | Detection (s) | Alarms | Root | Recovery result |
| --- | --- | ---: | ---: | --- | --- |
| Every-second-request intermittent fault, before timed bursts | INC-1013 | 140.2 | 3 | inventory (correct) | passed |
| CPU pressure, six-minute recovery limit | INC-1014 | 135.3 | 5 | inventory (correct) | timed out; all alarms later returned to OK and the incident was resolved |
| CPU pressure, eight-minute recovery limit before cancellation fix | INC-1016 | 135.9 | 5 | inventory (correct) | passed |

The first CPU timeout exposed queued CPU work continuing after the fault setting
was cleared. The later run on `ac7bf99` verifies the cancellation fix.

The CPU test produced measured inventory process CPU utilization above 94% in
CloudWatch's one-minute averages during the fault. It was detected by latency and
downstream error alarms; there is no separate CPU alarm. In an earlier run, clearing
the CPU setting left previously queued work running for several minutes and the
six-minute recovery gate expired just before the last alarm returned to OK. That
observation led to a code fix that lets the control endpoint clear the setting
promptly and stops already queued CPU work. In the final run, inventory was healthy
with a 10 ms probe soon after recovery; CPU utilization fell from about 96% during
the fault to 11.7% in the next minute and about 1.3% the minute after. All five
alarms returned to OK within the eight-minute recovery window.

All live diagnoses returned `UNAVAILABLE` because the AWS account's Bedrock daily
token quota was exhausted. The runner leaves AI cause correctness unscored in that
state. No claim about AI diagnostic accuracy follows from these runs. The matching
heuristic in `scenarios/run.py` also requires manual review if a future run returns
`READY` diagnoses.
