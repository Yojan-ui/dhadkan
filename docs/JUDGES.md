# Dhadkan for judges (statement 4.3)

**Problem.** A small powerloom unit, about six looms on average (3,85,596 units running 24,86,418
looms, 2013 baseline survey, [source](https://www.technicaltextiles.in/?p=29103)), loses a day's
output when a loom motor or pump seizes. The bearing that caused it costs a few hundred rupees.
Dhadkan hears the fault coming, names it, and tells the owner on WhatsApp what to fix, what it costs,
and how long they can wait. Only a reply of **1** books a mechanic. **Nothing ever stops a machine.**

**Run it (fresh clone):** `make setup && make demo-data && make demo`, then press **Demo mode**.
Demo mode needs no dataset download (its NASA bearing ships in `edge/models/ims_demo_pack.npz`).
The fallback if the internet fails is in [DEMO_SCRIPT.md](DEMO_SCRIPT.md).

## How each requirement is met

The official statement text is not in this repo. These are the requirements as we were briefed.

| requirement | where | status |
|---|---|---|
| Real sound data, normal-only training, AUC/pAUC per machine on the official test split | [data/real.py](../data/real.py), [edge/train.py](../edge/train.py), [edge/scoring.py](../edge/scoring.py) (pAUC exactly as the DCASE baseline code) | done; results below are weak for fan and valve |
| Real run-to-failure to calibrate days-to-failure and drive the replay | [edge/ims.py](../edge/ims.py) (leave-one-out calibration), [server/forecast.py](../server/forecast.py), [server/imsdemo.py](../server/imsdemo.py) | done; 4 failures only |
| Fault-type classifier trained only on CWRU labels | [edge/faulttype.py](../edge/faulttype.py) | done; vibration only, not microphone |
| Results document from real runs, failures included | [RESULTS.md](RESULTS.md) (generated), [results_notes.md](results_notes.md), [runs/](runs/) | done |
| Diagnosis card from a knowledge base, not LLM text: 7 faults, severity, repairable, part + ₹ + labour + technician, misuse, warranty, cost of waiting, sources | [server/faults.yaml](../server/faults.yaml), [server/diagnosis.py](../server/diagnosis.py) | done; labour hours and bearing warranty have no published source (marked null) |
| WhatsApp: Meta Cloud API with Twilio fallback, keys only in `.env` | [server/notify/whatsapp.py](../server/notify/whatsapp.py), [.env.example](../.env.example) | done; **no real send yet** (no keys) |
| Hindi / Marathi / English (+ Hindi in Roman script), 5 s before/now clip, spectrogram | [server/diagnosis.py](../server/diagnosis.py), [server/notify/media.py](../server/notify/media.py) | done |
| Signed reply webhooks, only "1" books, never stops a machine | [server/notify/service.py](../server/notify/service.py), [server/app.py](../server/app.py) `/webhooks/*`, [tests/test_guardrails.py](../tests/test_guardrails.py) | done, tested |
| Supervisor escalation after 2 h on Stop-now; offline queue with SMS fallback | [server/notify/service.py](../server/notify/service.py), [edge/outbox.py](../edge/outbox.py) | done, tested |
| README (problem, GIF, diagram, results, `make demo`, limits), pitch, Demo mode button | [README.md](../README.md), [PITCH.md](PITCH.md), [web/src/ui/DemoMode.tsx](../web/src/ui/DemoMode.tsx) | done |
| Tests: KB schema, severity, WhatsApp payload + webhook, no-stop guardrail, offline queue | [tests/](../tests/) (`make test`) | done |

## Real results

<!-- results:start -->
| what | measured | compared with |
|---|---|---|
| fan: fault vs normal sound (DCASE 2020 official test split) | AUC 55.3% · pAUC 51.2% | DCASE baseline 65.83% · 52.45% |
| pump: fault vs normal sound (DCASE 2020 official test split) | AUC 63.7% · pAUC 56.3% | DCASE baseline 72.89% · 59.99% |
| slider: fault vs normal sound (DCASE 2020 official test split) | AUC 78.3% · pAUC 62.3% | DCASE baseline 84.76% · 66.53% |
| valve: fault vs normal sound (DCASE 2020 official test split) | AUC 54.1% · pAUC 50.6% | DCASE baseline 66.28% · 50.98% |
| gearbox (MIMII DUE, source domain) | AUC 65.9% · pAUC 50.4% | – |
| fault type, CWRU bearing load never seen in training | accuracy 93.0% (mean of 4 loads) | 4 classes |
| fault type, CWRU fault size never seen (0.028 in) | accuracy 48.0% | failure case |
| NASA IMS 1st_test/b3 (inner race) | alarm 25.97 days before the end; 56/113 calibrated day ranges held the real remaining time | classifier said ball |
| NASA IMS 1st_test/b4 (ball) | alarm 25.97 days before the end; 142/163 calibrated day ranges held the real remaining time | classifier said ball |
| NASA IMS 2nd_test/b1 (outer race) | alarm 3.13 days before the end; 68/106 calibrated day ranges held the real remaining time | classifier said outer race |
| NASA IMS 3rd_test/b3 (outer race) | alarm 35.27 days before the end; 135/158 calibrated day ranges held the real remaining time | classifier said inner race |
| NASA IMS 2nd_test/b2 (did not fail) | 33.1% of readings at alarm | false alarms |
| NASA IMS 2nd_test/b3 (did not fail) | 82.7% of readings at alarm | false alarms |
| NASA IMS 2nd_test/b4 (did not fail) | 45.4% of readings at alarm | false alarms |

*Generated by `python -m edge.results`; details and sources in docs/RESULTS.md.*
<!-- results:end -->

## Limitations (read these before scoring)

- **Sound-only detection is weak.** On the official DCASE split, fan and valve are near chance and
  every type is below the DCASE baseline on most IDs (we trained on about a tenth of the data). In the
  scripted MIMII replay, the "faulty" blower is never warned about. Demo mode's real bearing is the
  replay where the score climbs to failure.
- **Fault naming needs vibration.** The classifier transfers from CWRU to the IMS bearing only partly,
  and in Demo mode it says inner race at the first alarm before settling on the true outer race.
- **Days-to-failure** rests on four real failures, and the ranges are wide (a factor of 10–20).
- **No Android app and no real WhatsApp send yet.** Prices are listed prices, not quotes. Downtime cost
  is an assumption. Factories and owners in the simulation are invented.
