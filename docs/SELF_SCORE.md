# Self-score as a strict 4.3 judge (2026-11-16)

Scored after building all ten spec sections and running the whole demo end to end from a fresh clone
(`make setup && make demo-data`, `make lint` with zero warnings, `make test`, then every stage live).
Evidence points at files; "missing" is what a strict judge would mark down.

| criterion | score | evidence | missing |
|---|---|---|---|
| Problem understanding (15) | **11** | Cited sector scale (2013 baseline via a Rajya Sabha reply), cited part and rewinding prices, one owner's story; the costs we could not source are labelled assumptions (PITCH, JUDGES) | No owner interviews and no field visit. The downtime cost per loom is an assumption, and the story's owner is invented |
| Statement fit (15) | **11** | Full loop: listen → understand → tell (5 languages, WhatsApp) → fix → prove; owner decides, no stop tool | Sound-only detection is weak: a loudness threshold beats the autoencoder on 15 of 19 units. The days countdown only works on vibration (IMS). No phone app |
| Technical depth (20) | **15** | Normal-only autoencoder with the DCASE baseline's exact pAUC; RMS baseline and edge cost; IMS leave-one-out calibration; CWRU geometry-normalised transfer; drift-safe adaptation; Claude Fable 5.2 with schema output, fallbacks, caching, cost log; Ed25519-signed records; offline queue; alert policy | The core detector loses to a trivial baseline, and we did not run the clean experiment that could fix it (choose a fusion on the dev split, test it on the unseen DCASE eval IDs). Claude is never called for real |
| Working implementation (20) | **15** | A fresh clone sets up, lints clean and passes 218 tests with only `make demo-data`; Demo mode, owner screen, mechanic job, repair verification, PDF, passport, forecast and onboarding all ran live; GIF recorded from the app | No real WhatsApp message sent, no Android app, no Claude call, templates not yet approved by Meta |
| Novelty (10) | **8** | Verified-repair proof (signed re-record) and a shareable health passport; cross-factory fingerprint registry (no audio); WhatsApp-first diagnosis card with cited rupees; owner's own words as weak labels | Each piece exists somewhere; the combination for a small shed is the new part |
| Usability for Bharat (10) | **8** | Hindi, Marathi, Gujarati, English and Hindi in Roman script; reply buttons; quiet hours, power cuts and a daily digest; onboarding by chat; big-type white owner screen; works offline except the send | No spoken alerts. Never tested with a real owner or on a low-end phone |
| Viability (10) | **6** | Technician/dealer subscription model; listed hardware cost per machine (₹5,000–6,100, cited); parts forecast as a dealer product; pilot plan | No willingness-to-pay data, plan prices unset, no partner shop. The breakdown cost the ratio rests on is assumed |
| **Total** | **74 / 100** | | |

## Gaps fixed in this pass, in order of points

1. A fresh clone could not run Demo mode or the dashboard. Now there's a committed IMS demo pack and
   `make demo-data` (+3–4, working implementation).
2. Owner language: Hindi in Roman script and Gujarati as full owner languages, plus a localised details
   reply (+2, usability).
3. Cited sector scale and hardware cost in the pitch (+1–2, problem and viability).
4. The owner screen's buttons take the WhatsApp reply path, so there's one decision path (+1).
5. Fresh-clone bugs found by running it end to end: Demo-mode language rejected by the API; leftover
   Demo readings after a restart; the time-machine test assuming a warning; the parts forecast counting
   a repaired machine.

## What would move the score next (needs people, keys or data)

- Real WhatsApp keys and one live send to a phone (working implementation, demo moment).
- An Anthropic key to run `scripts/eval_understand.py` (technical depth: Claude vs rules, ₹ per alert).
- Field recordings from one shed and two owner interviews (problem understanding, statement fit, viability).
- The DCASE 2020 evaluation IDs, to test a loudness + autoencoder fusion on data we have not seen.
