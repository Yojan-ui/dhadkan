# Demo script: 5–7 minutes

Follows the six slides in [PITCH.md](PITCH.md) with a live run of **Demo mode** in the middle. Every
number you say must be on screen, in [RESULTS.md](RESULTS.md), or in PITCH.md with its source or its
"assumption" label. Don't say anything else.

## Before you go on (10 minutes earlier)

1. `make demo` (opens http://127.0.0.1:8600). Fresh laptop: `make setup && make demo-data` first.
2. **Real WhatsApp** (optional): keys in `.env` (see README). Meta test number: the judge's number must
   be registered on it beforehand. Twilio sandbox: the judge sends the join code first. Without keys
   everything runs as a clearly labelled **dry run**.
3. Rehearse once with `DHADKAN_DRY_RUN=1 make demo` so nothing is sent by accident.
4. Have [media/demo.gif](media/demo.gif) open in a browser tab, as the last-resort fallback.

## The flow

| time | slide / screen | do | say (short) |
|---|---|---|---|
| 0:00–0:45 | PITCH 1: problem | — | A small powerloom unit (about six looms on average, cited). A 6205 bearing costs ₹65–180 (cited). An unplanned stop costs far more: in our demo profile, an **assumed** ₹1,56,960. |
| 0:45–1:30 | PITCH 2: insight | — | Bearings get louder for days first. Train on normal sound only. Name the fault from a cited table. Tell the owner on WhatsApp. Only "1" books a mechanic. |
| 1:30–1:50 | App | Click **Demo mode**, type the judge's number, pick **Hindi** or **Hindi (Roman script)**, press **Start**, then **Close**. | "This is a real bearing from NASA's test rig, run to destruction in 2004, playing as Loom 1." |
| 1:50–3:00 | App: Loom 1 panel | Point at the ring turning amber, then orange, the score climbing, and the days range. | "Flat for days, then the damage starts. The range is calibrated on the other failed bearings." |
| 3:00–3:40 | App: diagnosis card | Scroll to **Diagnosis**. Point at severity, part, ₹, labour, warranty and cost of waiting, and at "typical, confirm with dealer". | "None of this is generated text: it's a cited table. The classifier's first guess is inner race. It settles on outer race, which is what really failed. We show its mistakes." |
| 3:40–4:20 | Judge's phone / Log drawer | Ask the judge to reply **3**, then **1**. Open **Log**. | "3 sends the full card. 1 books the visit. Nothing here can switch a machine off." |
| 4:20–5:20 | PITCH 4: results | Open **Evaluation** (E) or RESULTS.md. | Read the generated table. Name the weak spots yourself: fan and valve are near chance on the official split; the IMS day ranges are wide. |
| 5:20–6:00 | PITCH 5: business | — | Repair shops and dealers pay, owners don't. Listed hardware is about ₹5,000–6,100 per machine. Plan prices are **not set**. |
| 6:00–6:40 | PITCH 6: roadmap | — | Pilot in one cluster, Android recorder, vibration clip, re-calibrate on pilot data. |
| 6:40–7:00 | — | — | Questions. |

## If the internet fails

Everything except the WhatsApp send runs offline: Demo mode's bearing ships with the repo, the models
run locally, and the web app is served from the laptop.

- **No network, keys set:** the alert shows **queued**. Say so, and show the built message in the
  **Log** drawer. It goes out when the network returns (offline queue, then SMS fallback).
- **No keys:** status **dry run**. Show the message in the Log drawer and the card in the panel.
  Simulate the reply in a terminal:
  `curl -X POST localhost:8600/api/alerts/reply -H 'content-type: application/json' -d '{"phone":"<number typed>","text":"1"}'`
- **The app won't start:** play [media/demo.gif](media/demo.gif) (recorded from this app) and walk
  the same story. Say it is a recording.
- **No WebGL (old laptop):** shrink the window below 768 px for the 2D layout.

Never say a message was delivered unless the status line says **sent**.
