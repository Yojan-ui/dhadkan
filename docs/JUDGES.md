# Dhadkan for judges (statement 4.3)

**Problem.** Small Indian factories (powerlooms, flour mills, pumps) run old machines until they
break. A typical powerloom unit is small: 3,85,596 units ran 24,86,418 looms in the 2013 baseline
survey ([source](https://www.technicaltextiles.in/?p=29103)), about six per unit. Dhadkan uses a phone
to hear failures coming, tells the owner on WhatsApp in their language, gets the right repair done,
and proves it worked. **Only the owner decides; no code path can stop a machine.**

**Run it (fresh clone):** `make setup && make demo-data && make demo`, then **Demo mode**. Phone
screens: `?view=owner&f=A&lang=hi` and `?view=mechanic&f=A`. If the internet fails, see
[DEMO_SCRIPT.md](DEMO_SCRIPT.md). How the parts fit together: [ARCHITECTURE.md](ARCHITECTURE.md).

## How each requirement is met

The official statement text is not in this repo. These are the requirements as we were briefed
(build spec sections 1–10).

| requirement | where | status |
|---|---|---|
| Listen: normal-only autoencoder, AUC/pAUC per machine on the official split | [edge/train.py](../edge/train.py), [edge/scoring.py](../edge/scoring.py) | done; weak for fan/valve (below) |
| RMS-threshold baseline; TFLite size and ms/window; battery | [edge/listen_eval.py](../edge/listen_eval.py) | done; **RMS wins on most units**; battery not measured (no app) |
| Learning period ("din 4/7"), drift-safe adaptation, noisy-shed flag | [edge/adapt.py](../edge/adapt.py), [server/agent.py](../server/agent.py) | done, tested |
| NASA IMS days-to-failure + replay; CWRU fault type | [edge/ims.py](../edge/ims.py), [edge/faulttype.py](../edge/faulttype.py) | done; 4 failures only |
| Understand: Claude Fable 5.2 (JSON schema, effort, fallbacks, caching, cost log), rules fallback | [server/understand.py](../server/understand.py) | built and tested with a fake client; **not run on the API** (no key) |
| Fable vs rules on 30+ labelled faults | [scripts/eval_understand.py](../scripts/eval_understand.py) | ready (48 CWRU cases); **needs a key** |
| Voice notes → Whisper → weak label on the alert | [server/owner_alerts.py](../server/owner_alerts.py) `on_voice`, [server/folk.py](../server/folk.py) | done, tested |
| faults.yaml: 7 faults, severity, repairable, fix + ₹ + labour + technician, misuse, warranty, sources | [server/faults.yaml](../server/faults.yaml), [server/diagnosis.py](../server/diagnosis.py) | done; labour hours and bearing warranty unsourced (null) |
| Tell: Meta Cloud API + Twilio, per-severity templates, buttons, hi/mr/gu/en + Roman Hindi, clip + spectrogram | [server/notify/](../server/notify/), [WHATSAPP_TEMPLATES.md](WHATSAPP_TEMPLATES.md) | done; **no real send yet**; spoken alerts not built |
| Approval gate, no stop tool, escalation 2 h, quiet hours, power cuts, daily digest, offline queue + SMS | [server/fix.py](../server/fix.py), [server/notify/policy.py](../server/notify/policy.py), [server/notify/service.py](../server/notify/service.py) | done, tested |
| Fix: mechanic job on "Mechanic bulao", dealer stock, weekly parts forecast (Message Batches) | [server/fix.py](../server/fix.py) | done; stock is mock data |
| Prove: re-record verification, signed repair record (PDF), mechanic success rate (owner only), passport, labels | [server/prove.py](../server/prove.py) | done, tested (tamper, privacy) |
| Setup: presets, factory profile, WhatsApp onboarding, 30 s baseline per machine | [server/onboard.py](../server/onboard.py), [data/presets.yaml](../data/presets.yaml) | done, tested |
| Screens: owner (white, big Devanagari), mechanic, 3D cluster | [web/src/screens/](../web/src/screens/), [web/src/scene/](../web/src/scene/) | done |
| Proof docs: RESULTS (incl. failures), field scoring script, field notes template | [RESULTS.md](RESULTS.md), [scripts/score_recording.py](../scripts/score_recording.py), [FIELD_NOTES.md](FIELD_NOTES.md) | done; no field recordings yet |
| Tests: guardrail, approval gate, queue, payloads + webhooks, escalation, quiet hours, i18n, schema, verification, tamper, privacy | [tests/](../tests/) | all pass (`make test`) |

## Real results

<!-- results:start -->
| what | measured | compared with |
|---|---|---|
| fan: fault vs normal sound (DCASE 2020 official test split) | AUC 55.3% · pAUC 51.2% | DCASE baseline 65.83% · 52.45% |
| pump: fault vs normal sound (DCASE 2020 official test split) | AUC 63.7% · pAUC 56.3% | DCASE baseline 72.89% · 59.99% |
| slider: fault vs normal sound (DCASE 2020 official test split) | AUC 78.3% · pAUC 62.3% | DCASE baseline 84.76% · 66.53% |
| valve: fault vs normal sound (DCASE 2020 official test split) | AUC 54.1% · pAUC 50.6% | DCASE baseline 66.28% · 50.98% |
| loudness (RMS) threshold vs our autoencoder, same test clips | RMS has the higher AUC on 15 of 19 units | failure case |
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

- **Sound-only detection is weak**, and a plain loudness threshold beats our autoencoder on most units.
  In the scripted MIMII replay, the "faulty" blower is never warned about. Demo mode's real bearing is
  the replay where the score climbs to failure.
- **Fault naming needs vibration**, and transfer from CWRU to IMS is partial: inner race at the first
  alarm, then outer race.
- **Days-to-failure** rests on four real failures, and the ranges span a factor of 10–20.
- **Not done:** an Android app, a real WhatsApp send, a Claude evaluation (needs a key), spoken alerts,
  field recordings, owner interviews. Prices are listed prices, downtime cost is an assumption, and
  every factory and owner in the demo is invented.
