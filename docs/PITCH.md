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
4. At the alarm, the classifier line appears ("outer race", and the IMS readme says outer race).
   The diagnosis card opens with part, ₹, labour, warranty and the cost of waiting.
5. The judge's phone buzzes. Ask them to reply **1**. The Log drawer shows the signed webhook, and
   the technician visit is booked.

**Say:**
> "This isn't an animation. It is the vibration of a real bearing that failed on a test rig in
> Cincinnati, replayed as Loom 1. Our model flags it, the classifier names it outer race, and the
> owner gets this, in Hindi, with the sound before and now. They reply 1. That's the only thing that
> books a mechanic, and nothing here can switch a machine off."

**If there are no keys or no network:** the status line says "dry run" or "queued". Say so. Don't
pretend a message went out. Show the built message in the Log drawer instead.

## 4. Results: measured on public benchmarks, failures included

<!-- results:start -->
*Not generated yet: run `make train` (or `make results` once the records exist).*
<!-- results:end -->

**Say:** read the block above as it stands. Then name one place it fails (the weakest machine ID,
or the unseen fault size from RESULTS.md) before a judge asks.

## 5. Business model: technicians and dealers pay, owners don't

**Say:**
> "The owner of a 20-loom shed won't pay for software. The people who earn from a repair will. A
> local motor-rewinding shop or bearing dealer subscribes, installs a phone in each customer's shed,
> and gets the alerts, the diagnosis card and the route for the day. Every 'reply 1' is a booked
> job for them. A dealer gets demand for the exact part number before the customer walks in."

- **Technician plan:** per shed per month, sold by the repair shop as part of an AMC (annual
  maintenance contract).
- **Dealer plan:** per district, with a part-demand forecast from the alerts.
- **Owner:** free alerts. They pay the technician as they already do.

Prices for these plans are **not set**. We have no willingness-to-pay data yet. The next step is to
ask ten rewinding shops in one cluster. Don't quote a price on stage.

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
