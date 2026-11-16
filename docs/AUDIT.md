# PS 4.3 audit and what changed (2026-11-16)

Tech4Bharat 2026, problem statement 4.3: *small factories lose production to breakdowns they cannot
see coming.* The requirements we audited against: learn from weeks of normal, unlabelled operation;
detect deviations before a breakdown; work offline; affordable hardware; help the owner plan
maintenance; production-stop decisions stay with the owner; recorded or simulated signals where there
is no hardware. The statement is marked AGENTIC AI.

## Audit before the changes

| area | state | finding |
|---|---|---|
| Sound anomaly detection | worked, weak | one autoencoder per machine *type*; below the DCASE baseline; a loudness threshold beat it on 15 of 19 units |
| Learning each machine's normal | built, not used | `edge/adapt.py` existed but nothing in the live path called it; alarm levels were frozen at training time; no versions, no owner-approved recalibration |
| Other unsupervised methods | missing | no MFCC, Isolation Forest or spectral-deviation detector, no comparison |
| Health engine | partial | risk ratio + trend + noisy-shed flag; nothing combined quality, history, repairs and confidence |
| RUL | thin | IMS leave-one-out with coverage; no MAE/RMSE, no "not enough data", no gap/restart handling; said "days to failure" |
| Agent | not agentic | a daily digest (rank, route, draft); no per-machine memory, no persistent plan, no recovery |
| Offline | partial | outbox with idempotency keys; no network status, no conflict rule, no sync of decisions |
| Hardware | gap | phone audio only, no device ingest, no ESP32 |
| Evaluation | partial | AUC/pAUC and coverage only; no precision/recall/F1/FAR, detection latency, MAE/RMSE or workflow metrics |
| Demo | powerloom cluster | not the flour-mill story the brief asks for |

Kept as they were (working, not extended): the type-level autoencoder, the CWRU classifier, WhatsApp
and SMS sending, the signed repair record and passport, onboarding, the 3D cluster screen.

## What each phase changed

| phase | files | result (records in `docs/runs/`) |
|---|---|---|
| 2. Per-machine normal | `edge/summary.py`, `edge/baseline.py`, `tests/test_baseline.py` | learning period, baseline v1 from that machine only, versions, drift check that cannot learn a fault, owner-approved recalibration, readings during cases / after restarts / off / unusable never learned |
| 3. Unsupervised detectors | `edge/detectors.py`, `edge/compare.py`, `tests/test_detectors.py` | six detectors; chosen on selection units, reported on held-out units: **loudness alone wins**; no fusion ships (RESULTS 1c) |
| 4. Health and time to threshold | `server/health.py`, `edge/rul.py`, `tests/test_health.py` | status, urgency, confidence with reasons, plain-word evidence; time to *maintenance threshold* with interval and explicit "not enough data"; IMS leave-one-out MAE/RMSE/coverage (RESULTS 3b) |
| 5. Agent | `server/maint/store.py`, `server/maint/agent.py` | persistent case per anomaly: recall → plan → act → verify; approval gate in code; no stop tool; hash-chained audit |
| 6. Offline-first | `server/maint/sync.py`, agent action queue | local-first; queued actions with backoff sent once; audit replicated idempotently; first-decision-wins conflicts asked again |
| 7. Technicians and parts | `server/maint/tools.py`, `data/demo/technicians.json` | technician search with fallbacks, stock check, cited cost ranges; demo/mock data labelled on every result |
| 8. Demo, screen, hardware | `server/flourmill.py`, `server/mill_api.py`, `web/src/screens/Mill.tsx`, `server/ingest.py`, `hardware/`, `tools/sim_device.py` | flour-mill story end to end on recorded readings (RESULTS 5); `?view=mill`; device ingest with source labels; ESP32 sketch (untested) |
| 9. Evaluation and docs | `server/maint/evaluate.py`, `edge/bench.py`, `edge/results_more.py`, this file | agent under 200 fault-injection runs (RESULTS 6); per-reading cost (RESULTS 7) |

## Bugs the evaluation found (fixed, with tests)

- A booking queued while offline was sent after the owner's approval had turned into a conflict
  (11 of 200 runs). Approvals are now re-checked at send time; the action is held until re-approved.
- A conflicting answer after the approval step crashed the agent's tick. It now goes back to waiting.
- A late approval booked a visit day that had already passed. The visit day is re-planned.
- After a repair, health judged the machine on pre-repair readings; questions stayed open after a case closed.

## Still not done (needs people, hardware or keys)

Field recordings and owner interviews; a phone app and phone/ESP32 timings; a real WhatsApp send;
Claude's diagnosis evaluated with a key; detector choice re-checked on fresh field data.
