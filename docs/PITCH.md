# Dhadkan: six-slide pitch script

About 3 minutes. Every number is either from a logged run in `docs/runs/`, from a source cited in
`server/faults.yaml`, or marked **assumption**. The results block on slide 4 is written by
`python -m edge.results`. Don't read numbers out of this file that aren't in it.

---

## 1. Problem: a seized motor costs a day, a bearing costs a few hundred rupees

**On screen:** one loom, a ₹ figure for the part, a much larger ₹ figure for the stopped day.

**Say:**
> "In Bhiwandi or Malegaon, a powerloom unit runs on cheap motors and bearings. A 6205 bearing costs
> ₹65 to ₹180 online. Rewinding a burnt motor runs about ₹1,000 per horsepower. Those prices are
> cited. What costs real money is the day the loom stands still: no output, workers paid to wait,
> and a part bought in a hurry. With the assumed costs in our demo profile, one unplanned stop of
> a loom drive costs about ₹1.57 lakh. The owner usually finds out when the motor is already smoking."

**Scale (cited):** India's powerloom sector had 3,85,596 units running 24,86,418 looms and employing
44,18,240 workers, per the 2013 baseline survey quoted in a Rajya Sabha reply by the MoS Textiles
([technicaltextiles.in report](https://www.technicaltextiles.in/?p=29103), accessed 2026-11-16).
That works out to about 6.4 looms per unit on average, so a typical unit is very small. No newer
census exists; the Ministry is still preparing a new baseline survey.

**Sources / labels:**
- 6205 bearing ₹65–180: `moglix_6205` in faults.yaml.
- Rewinding ₹1,000/HP: `indiamart_rewind`, with `bhel_rewind_2018` for the HP bands.
- ₹1,56,960 per unplanned stop is an **assumption**: `data/sim/costs.json`, factory A, gearbox
  drive. It is 24 h down × (₹6,000 output/h + 6 idle workers × ₹90/h), as computed by
  `server/costs.breakdown`. We found no published per-loom downtime figure. Say "assumed", and
  invite the judge to plug in their own number.

## 2. Insight: the machine tells you days before, and the owner already has a phone and WhatsApp

**On screen:** the health-score curve of the real IMS bearing, flat for days, then rising.

**Say:**
> "Bearings don't fail at random. They get louder for days first. You don't need a vibration lab
> to hear it: a model that knows what *this* machine sounds like when healthy notices when it
> drifts. And the owner doesn't need a dashboard. They need one WhatsApp message in Hindi or
> Marathi that says what is wrong, what it costs to fix today, and what it costs to wait."

Three ideas, one line each:
- Train on **normal** sound only, because every factory has plenty of that and almost no recorded
  faults.
- **Name** the fault with a classifier trained on labelled bearing faults (CWRU). Price the fix
  from a cited table, not from free text.
- The owner decides. **Only "1" books a mechanic. Nothing ever stops a machine.**

## 3. Demo moment: the fault grows in replay and a WhatsApp alert arrives live

**Do:**
1. Before you start, put the judge's number into **Demo mode**. On a Meta test number, that number
   must already be registered. On the Twilio sandbox, the judge sends the join code first.
2. Press **Start Demo mode**. Loom 1 now replays NASA IMS test 2, bearing 1: a real bearing run to
   destruction in February 2004.
3. Let the ring go amber, then orange. Point at the score climbing and at the days range shrinking.
4. At the alarm, the diagnosis card opens: bearing wear, with part, ₹, labour, warranty and the
   cost of waiting. The classifier line under Demo mode shows its live guess at the bearing fault.
   At the first alarm it says inner race (wrong). As the damage grows it settles on outer race,
   which is the truth per the IMS readme. Point at both: the card is right either way, because
   every bearing fault maps to bearing wear.
5. The judge's phone buzzes. Ask them to reply **1**. The Log drawer shows the signed webhook, and
   the technician visit is booked.

**Say:**
> "This isn't an animation. It is the vibration of a real bearing that failed on a test rig in
> Cincinnati, replayed as Loom 1. Our model flags it as bearing wear, and the owner gets this, in
> Hindi, with the sound before and now. Watch the classifier: it guesses inner race at first, then
> settles on outer race, which is what really failed. We show its mistakes too. They reply 1. That's the only thing that
> books a mechanic, and nothing here can switch a machine off."

**If there are no keys or no network:** the status line says "dry run" or "queued". Say so. Don't
pretend a message went out. Show the built message in the Log drawer instead.

## 4. Results: measured on public benchmarks, failures included

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

**Say:** read the block above as it stands. Then name one place it fails (the weakest machine ID,
or the unseen fault size from RESULTS.md) before a judge asks.

## 5. Business model: technicians and dealers pay, owners don't

**Say:**
> "The owner of a small powerloom unit, about six looms on average, won't pay for software. The people who earn from a repair will. A
> local motor-rewinding shop or bearing dealer subscribes, installs a phone in each customer's shed,
> and gets the alerts, the diagnosis card and the route for the day. Every 'reply 1' is a booked
> job for them. A dealer gets demand for the exact part number before the customer walks in."

- **Technician plan:** per shed per month, sold by the repair shop as part of an AMC (annual
  maintenance contract).
- **Dealer plan:** per district, with a part-demand forecast from the alerts.
- **Owner:** free alerts. They pay the technician as they already do.

Prices for these plans are **not set**. We have no willingness-to-pay data yet. The next step is to
ask ten rewinding shops in one cluster. Don't quote a price on stage.

**Hardware per machine (listed prices, accessed 2026-11-16; the Android app is not built, so none of
this is tested):**
- An entry-level Android phone as the listener: ₹4,990 (Lava Z22) to ₹5,799 (itel A05s), per the
  [GizNext under-₹6,000 list](https://www.giznext.com/mobiles/top-phones-below-6000-price-list).
- Optional vibration channel: an MPU-6050 accelerometer module, ₹119
  ([Rytronics](https://rytronics.in/product-tag/imu)) to ₹275
  ([Zbotic](https://zbotic.in/product/gy-521-mpu-6050-module-without-soldering/)). It also needs a
  microcontroller to read it, which is not costed here.
- So about ₹5,000–6,100 per machine before installation. Against the **assumed** ₹1,56,960 cost of one
  unplanned stop (slide 1), that is 3.3–3.9% of one avoided breakdown. The ratio is only as good as that
  assumption.

## 6. Roadmap

1. **Pilot:** 10 looms in one cluster, one partner rewinding shop, 8 weeks. Collect real loom audio,
   owner replies and repair outcomes. Every confirmed repair becomes a labelled fault.
2. **Phone app:** the `.tflite` model and log-mel front end are exported and checked. Build the
   Android recorder around them.
3. **Low-cost vibration clip:** a magnetic accelerometer so the fault classifier runs on the same
   signal it was trained on (today it needs vibration, not sound).
4. **Re-calibrate days-to-failure** on pilot data, which gives more than the four IMS failures.
5. **Prices from partner dealers** replace the listed-price table, still shown as "confirm with
   dealer".

**Close:**
> "Dhadkan means heartbeat. Every machine has one, and we just listen for when it changes."
