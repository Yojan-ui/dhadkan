# Architecture

Five stages: **Listen → Understand → Tell → Fix → Prove**. Each one works without the next. The owner
makes every decision, and **no code path can stop a machine** (`tests/test_guardrails.py`).

```mermaid
flowchart LR
  subgraph Edge["Phone (edge/, offline)"]
    MIC["mic, 10 s readings"] --> FE["log-mel windows<br/>features.py"] --> AE["autoencoder .tflite<br/>runtime.py"]
    AE --> AD["learning period, drift-safe level,<br/>noisy-shed flag<br/>adapt.py"]
    AD --> OB["offline outbox<br/>outbox.py"]
  end
  subgraph Server["server/ (FastAPI + WebSocket, 2 Hz)"]
    OB --> SIM["readings<br/>sim.py, app.py"]
    SIM --> FC["days to fault level<br/>forecast.py (IMS-calibrated)"]
    SIM --> DX["diagnosis card<br/>diagnosis.py + faults.yaml"]
    VIB["vibration (optional)<br/>faulttype.py (CWRU)"] --> DX
    SIM -. "only when the fault<br/>is not named" .-> UN["Claude Fable 5.2<br/>understand.py"] --> DX
    DX --> OA["owner_alerts.py"]
    OA --> POL["quiet hours, power cuts,<br/>daily digest<br/>notify/policy.py"]
    POL --> NT["notify/service.py<br/>WhatsApp → SMS → queue"]
    NT --> WA["notify/whatsapp.py<br/>Meta Cloud API / Twilio"]
    WA -->|"reply 1 / button"| FIX["fix.py: mechanic job<br/>(needs logged approval)"]
    WA -->|"voice note"| FOLK["folk.py (Whisper)<br/>weak label"]
    FIX --> PROVE["prove.py: re-record,<br/>signed record, passport"]
    WA -->|"Namaste"| ONB["onboard.py<br/>presets.yaml"]
  end
  subgraph Web["web/ (React + Vite + r3f)"]
    D3["cluster screen (desktop, black)"]
    OW["owner screen (phone, white)"]
    ME["mechanic screen (phone, white)"]
  end
  Server --> Web
```

## Modules

| stage | module | does | needs |
|---|---|---|---|
| Listen | `edge/features.py`, `edge/model.py`, `edge/train.py` | log-mel windows, conv autoencoder trained on normal sound only, TFLite export, evaluation | DCASE 2020 / MIMII DUE |
| | `edge/runtime.py` | the `.tflite` as the phone runs it: score, per-band deviation, fingerprint | |
| | `edge/adapt.py` | learning period, alarm level, drift-safe adaptation, noisy-shed flag | |
| | `edge/listen_eval.py` | RMS-threshold baseline, edge cost (size, ms) | |
| | `edge/ims.py`, `edge/faulttype.py` | NASA IMS run-to-failure calibration; CWRU fault-type classifier | IMS, CWRU |
| Understand | `server/diagnosis.py`, `server/faults.yaml` | severity rules and the diagnosis card; every ₹, part and warranty value is cited here | |
| | `server/understand.py` | Claude Fable 5.2 second look (JSON schema, effort high, fallbacks, caching, cost log) | `ANTHROPIC_API_KEY` (optional) |
| | `server/folk.py` | owner's words / voice notes → weak label (local Whisper, lexicon, Claude for unknown words) | |
| Tell | `server/owner_alerts.py` | builds the alert from the card, media, sends; replies, voice notes, onboarding | |
| | `server/notify/policy.py` | quiet hours, power cuts, daily digest, templates, buttons | |
| | `server/notify/service.py` | alert log, outbox, WhatsApp → SMS fallback, escalation after 2 h, release of held alerts | |
| | `server/notify/whatsapp.py` | Meta Cloud API and Twilio providers, signed webhooks, media download | keys in `.env` |
| Fix | `server/fix.py` | mechanic job (refused without logged approval), parts forecast, dealer batch | |
| Prove | `server/prove.py` | repair verification, Ed25519-signed record (PDF), health passport, owner-only mechanic stats | `keys/` (auto-created) |
| Setup | `server/onboard.py`, `data/presets.yaml` | WhatsApp onboarding, presets, factory profile, baseline clips | |
| Demo | `server/sim.py`, `server/imsdemo.py`, `server/agent.py`, `server/route.py` | 3 simulated factories replaying real recordings; Demo mode (NASA IMS); route planning | |

## Per-machine learning and the maintenance agent (PS 4.3 pass, 2026-11-16)

```mermaid
flowchart LR
  subgraph Node["Factory node (phone / shed PC): works offline"]
    DEV["phone mic / ESP32 vibration /<br/>upload / simulated<br/>server/ingest.py"] --> SUM["reading summary + quality<br/>edge/summary.py"]
    SUM --> BL["this machine's baseline<br/>learning, versions, drift guard<br/>edge/baseline.py"]
    BL --> DET["detector chosen on selection units<br/>edge/detectors.py, edge/compare.py"]
    DET --> HE["health: status, urgency, confidence,<br/>time to threshold<br/>server/health.py, edge/rul.py"]
    HE --> AG["maintenance agent<br/>case, plan, approvals, audit<br/>server/maint/agent.py"]
    AG --> TL["technicians, parts, cited costs<br/>server/maint/tools.py"]
    AG --> Q["action queue (sent once)"]
  end
  Q -->|"when online"| CL["cloud: audit replica,<br/>owner replies (WhatsApp)<br/>server/maint/sync.py"]
  CL -->|"decisions"| AG
  AG --> UI["?view=mill<br/>web/src/screens/Mill.tsx"]
```

| module | does |
|---|---|
| `edge/summary.py` | one reading → 254 numbers (loudness, mel band statistics, MFCC) + quality (clipped, dropout, too short); never audio |
| `edge/detectors.py` | RMS, spectral shape, transient, MFCC-Mahalanobis, Isolation Forest, calibrated autoencoder; each fitted on one machine's normal readings, out-of-fold calibrated; explanation of what changed |
| `edge/compare.py` | chooses the detector on selection units, reports on held-out units |
| `edge/baseline.py` | per machine: learning period, baseline versions, readings never learned (high, during a case, after a restart, off, unusable), small drift adapts only if clear anomalies stay flagged, large drift needs the owner |
| `edge/rul.py` | time to the maintenance threshold with a calibrated range; explicit insufficient-data, no-trend and at-threshold states; IMS leave-one-out evaluation |
| `server/health.py` | readings → status, urgency (monitor, measure again, inspect, maintenance, urgent owner attention), confidence with named reasons, plain-word evidence, next check |
| `server/maint/store.py` | the agent's memory: machines, cases, steps, idempotent actions, approvals, repairs, hash-chained audit |
| `server/maint/agent.py` | one case per anomaly; steps persisted (restart resumes); gated paid actions; recovery from network, provider, technician and crash failures; verification against baseline v1 |
| `server/maint/sync.py` | idempotent push of the audit chain (tampering refused), pull of the owner's WhatsApp decisions, first-decision-wins conflicts asked again |
| `server/maint/evaluate.py` | 200 randomised fault-injection runs of the workflow |
| `server/flourmill.py`, `server/mill_api.py` | the end-to-end story on recorded readings; `/api/mill/*` |
| `server/ingest.py`, `hardware/`, `tools/sim_device.py` | device readings labelled live / upload / simulated, idempotent per device sequence number |

Guardrails added in this pass (tested): `book_job` and `order_part` refuse without an approved
approval of the right kind for the same case, checked when created and again when sent; questions
expire when a case closes; a stop is only ever a question to the owner, whose answer is logged.

## Data and state

- **Committed:** trained `.tflite` models and per-machine thresholds (`edge/models/`), the IMS demo pack, every evaluation
  record cited anywhere (`docs/runs/`), the knowledge base, presets and simulation configs (all assumptions labelled).
- **Downloaded, git-ignored:** datasets (`data/raw/`, `make data` or `make demo-data`).
- **Local state, git-ignored:** SQLite `data/node.db` (readings, messages, alerts, repair records), registry, outbox,
  `data/alerts/` (alert media), `data/factories/` (onboarded profiles and baseline clips), `data/labels/` (verified
  repairs), `data/llm/` (Claude call log), `runs/field/` (field scores), `keys/` (signing key), `.env` (API keys).
- **Never stored or shared:** raw audio outside the factory's own computer. The registry holds 64-number fingerprints,
  and the passport holds numbers only.

## Guardrails (enforced in code, tested)

1. No tool, endpoint or reply can stop or control a machine.
2. No outward action without the owner's logged approval: the mechanic's job is refused without `messages.approved = 1`.
3. Only reply `1` (or button 1) books; everything else is logged.
4. Prices, parts and warranty come from `faults.yaml` with sources, marked "typical, confirm with dealer". Claude never
   writes a number on the card.
5. Keys only in `.env`; the recorder and rehearsals can force a dry run (`DHADKAN_DRY_RUN=1`).
