## 4. What works, what doesn't

These notes are written by hand. Every number they refer to is in the generated tables above.

### Anomaly detection on sound (DCASE 2020, official test split)

- **Slider works best.** It is the only machine type where the model separates faults well on most
  IDs, and on its best ID it is close to the official baseline.
- **Pump depends on the unit.** One ID separates well, another is at chance.
- **Fan and valve are near chance on every ID.** An earlier version of this project reported fan
  AUC 0.99. That was on a 40-clip MIMII subset; on the full official test split it does not hold.
  Valve faults are short clicks, and averaging reconstruction error over a 10 s clip washes them out.
- **Below the DCASE baseline on most IDs.** The baseline trained on every normal clip; we used the
  first 100 per ID (download bandwidth). We did not tune the scoring on the test split to close the gap.
- **A plain loudness threshold beats our autoencoder on most units** (section 1b). Each clip's
  loudness against the machine's own normal separates faults better than reconstruction error on
  the official split. Part of this may be a recording artifact (faulty clips recorded at different
  levels), and in a real shed loudness also moves with background noise (the noisy-shed flag in
  edge/adapt.py exists for that). We have not switched detectors: any combination chosen now would
  be tuned on the test split we already looked at. It needs fresh data, from field recordings.
- **Consequence on the dashboard.** A machine's countdown only appears when its recorded faults score
  above its alarm level. On these models that holds for only two of the 19 units, and for none of the
  units the scripted MIMII story replays (blowers on fan id_00, pumps on id_00/id_02, gearbox drives).
  So in that story the "faulty" blower is never warned about, and the alarms that do fire are healthy
  machines crossing their alarm level (about 1 reading in 10, by the alarm-level design). Demo mode's
  NASA IMS bearing is the one replay where the score really climbs to failure.

### Works

- **The alarm fires on every IMS bearing that failed**, days before the end. On test 2 (the Demo-mode
  bearing), the score stays flat around 1 for the first ~3.7 days. It then rises steadily over about
  3 days to failure.
- **The CWRU-trained classifier names the Demo-mode bearing correctly** (outer race, test 2 bearing 1)
  from the last 5% of its vibration, and also the test 1 ball/roller defect. It never saw a Rexnord
  bearing or a 20 kHz signal. It transfers only through defect frequencies computed from the
  bearing's geometry.
- **Calibrated day ranges are honest about how little four failures can tell you.** The raw trend
  ranges almost never contain the real remaining time. Widened by the error ratios measured on the
  other bearings, they contain it roughly as often as a 10–90% range should on two of four held-out
  bearings, and less often on the other two. The price is width: a range spans about a factor of 10–20.

### Doesn't work (failure cases)

1. **Healthy bearings on the same shaft alarm too.** In IMS test 2, bearings 2–4 did not fail, but
   their scores rise far above the alarm level in the last two days as bearing 1 breaks up and
   shakes the whole shaft. Bearing 3 also drifts slowly above 1.0 from about day 1, *before* the
   failing bearing alarms. A per-bearing alarm on a shared shaft cannot tell "my bearing" from "my
   neighbour's bearing". A dashboard should rank by score, not just by alarm.
2. **Restarts look like faults.** IMS test 1 was stopped overnight and on weekends. Both of its
   bearings first alarm on the first reading after the rig restarts on day 8.5 (the same reading for
   both), not because of wear. The baseline (first 10% of snapshots) covers only the first two short
   sessions, so a level shift after restarting reads as change. A real install has to re-baseline after maintenance or a long stop.
3. **Wear comes in steps, and trends can't predict a step.** Test 3 bearing 3 runs on a plateau for
   about a month and then fails within the last two days. Test 1 bearing 4 jumps, settles and jumps
   again. A straight line through the last 1–2 days misses both ways. Calibration widens the range
   but doesn't fix this.
4. **Failure level varies ~20× between bearings.** One bearing's "failed" score says little about
   another's. That is why the borrowed level puts the Demo-mode bearing "at fault level" more than a
   day before it actually stopped.
5. **The classifier names the Demo-mode fault late.** Evaluated on the last 5% of life it says outer
   race (correct). In Demo mode it classifies the last hour of vibration at every reading after the
   alarm: at the first alarm it says inner race with high confidence (wrong), and it settles on outer
   race about a day later. The diagnosis card is bearing wear either way. Demo mode also starts after
   the health score's learning window: the bearing's run-in transient sits inside that window and
   would otherwise raise an alarm on the very first reading.
6. **The classifier gets two of four IMS failures wrong**: test 1 inner race → ball, and test 3
   outer race → inner race. On CWRU it fails completely on the unseen 0.028″ fault size (every
   inner-race window called ball). With one training bearing, faults it has not seen are guesses,
   and the card says "named by the CWRU-trained classifier" so the owner knows it isn't an inspection.
7. **Microphone audio is not vibration.** None of the IMS/CWRU results say anything about phone
   audio. The anomaly model on DCASE sound is evaluated separately in section 1.
