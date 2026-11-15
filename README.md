# Dhadkan

![Spectrograms of a real industrial blower, normal and faulty](docs/media/blower_spectrograms.png)

Listen: [blower, normal](docs/media/blower_normal.wav) · [same blower model, faulty](docs/media/blower_fault.wav)
(10 s each, MIMII dataset fan id_00, CC BY-SA 4.0)

**A small factory in India loses a day's output when a pump or blower seizes, and the owner had no
way to hear it coming.**

Dhadkan listens to each machine through a phone, flags a sound that is drifting away from that
machine's own normal, and sends the owner a short note a mechanic would write:

> ब्लोअर 1: बेयरिंग की आवाज़ 2 दिन से बढ़ रही है। मंगलवार से पहले जाँच करवा लें। रमेश को मंगलवार को भेजें? जवाब दें: हाँ / ना

When one owner confirms a fault ("खट-खट आ रही है"), its sound fingerprint (64 numbers, never audio)
goes to a shared registry. Machines of the same model in other factories are re-checked against it
and can be flagged before their own alarm trips. Nothing ever stops a machine: the owner replies
yes or no to a technician visit.

Built for Tech4Bharat problem statement 4.3 (machine failure prediction for small factories).

---

## Run it

```bash
make setup     # .venv (Python 3.12) + pip install, and npm ci for the web app
make data      # ~1 GB subset of MIMII + MIMII DUE, about 30 min (python -m data.download --small halves it)
make train     # 3 models -> edge/models/, evaluation -> docs/runs/eval_<UTC>.json (~2 min CPU)
make demo      # builds the web app, serves it with the API on :8600, starts the demo replay, opens it
```

The trained models and the evaluation record are committed, so `make setup && make demo` works
without `make data` / `make train` except for the audio replay itself, which needs the clips.

```bash
make test      # pytest: scoring, thresholds, registry, outbox, agent guardrails, live server
make lint      # ruff + black, eslint + prettier, tsc --strict; zero warnings
make dev       # API on :8600 + Vite with hot reload on :5173
python -m server.folk    # optional: Whisper check on the sample voice notes -> docs/runs/asr_<UTC>.json
python -m server.story   # the demo script without waiting -> docs/runs/story_<UTC>.json
```

Add `?debug` to the URL for frame rate and draw calls, and `?m=B-F1` to open a machine directly.

Everything runs offline after the downloads (datasets; the Whisper `small` model is fetched from
Hugging Face on first use). Only two things call the network, both optional and both need
`ANTHROPIC_API_KEY`: the agent's "Run (Claude)" button and the LLM fallback for sound words the
lexicon doesn't know. Without a key, "Run (rules)" calls the same tools in a fixed order.

The registry API on its own: `uvicorn server.registry_api:create_app --factory --port 8100`.

## Demo

Switch on **Demo replay** (top right). It resets the cluster and plays one reading every 10 s, each
one a real 10 s MIMII clip scored by the exported `.tflite` model and streamed over a WebSocket at
2 Hz (the log-mel slice, the 1 s window's anomaly score and loudness under the play cursor):

1. **0:00** 3 sheds, 12 machines. Each machine shakes in proportion to its live window score, its
   floor ring fills clockwise toward its own alarm level (grey, amber near alarm, orange at alarm or
   on a registry match), and ripples rise with the clip's loudness.
2. **0:40** scripted fault: factory A's blower (A-F1) starts replaying abnormal recordings.
3. **0:50** the owner's Hindi voice note ("ब्लोअर में खट-खट की आवाज़ आ रही है") is transcribed by
   local Whisper; "khat-khat" maps to *bearing* through the lexicon (weak label).
4. **1:10** the fingerprint is published. Arcs go from A-F1 to every blower in the other sheds,
   labelled with the registry's actual cosine against each machine's flag level. B-F1 (the same
   blower model) then gets a *synthetic* fault onset.
5. When B-F1 matches in 2 of its last 3 readings, its arc is redrawn and its ring pulses, the rules
   agent runs, and the technician's route draws itself with arrival times.
6. The bottom bar shows the owner's SMS (Marathi for factory B, Hindi for A) with an English line
   under it. **हो / Ho** books the visit and the technician marker drives the route.

Click a shed to fly into it, then a machine: the camera orbits it beside a live 3D waterfall
(mel frequency x time x level; bands more than 3 std from that machine's normal in orange), a
7-day risk trend, and **Play live sound** / **Play normal reference** (a held-out normal clip of
the same recorded unit). **Operator** has the same steps as buttons, including **Run (Claude)**
when `ANTHROPIC_API_KEY` is set; **Evaluation** shows the scores below; **Log** lists every agent
tool call. Under 768 px wide the page switches to a 2D list with the same rings and never loads
three.js. With `prefers-reduced-motion`, nothing shakes, ripples or tweens.

In the logged run of the same script (`python -m server.story` ->
`docs/runs/story_20261115T153252Z.json`), B-F1 was flagged at onset strength 0.2, three readings
(one simulated day) before its own alarm. That is one replay, not a statistic; the statistic is the
registry table below.

If the shed has no signal when the owner confirms, the fingerprint waits in the phone's outbox
(`edge/outbox.py`: SQLite, retried with backoff, idempotency key so a retry after a lost reply is
stored once) and goes out when the signal returns. **Operator → Shed signal** shows this.

## What is in here

```
edge/        the model, and what the phone runs
  features.py   audio -> 1.06 s log-mel windows (64 mels x 32 frames, 50% overlap)
  model.py      conv autoencoder (217k params) + TFLite export
  train.py      trains on NORMAL windows only, per-machine thresholds, AUC/pAUC, registry check
  scoring.py    reconstruction error, thresholds, AUC, DCASE pAUC
  runtime.py    the .tflite model + per-machine alarm levels, as the phone would run it
  embed.py      fault fingerprint: per-band residual, standardised per machine
  outbox.py     offline queue for fingerprints (exactly once, in order, survives restarts)
  models/       exported .tflite + meta.json per machine type
server/      API, registry and agent
  app.py        FastAPI + WebSocket for the web app, 2 Hz stream, demo replay script
  demo.py       `make demo` launcher
  registry.py   SQLite: fingerprints and weak labels (no audio column exists)
  registry_api.py  FastAPI: /fingerprints, /match, /labels/text, /labels/voice
  folk.py       Whisper (local) -> lexicon -> Claude fallback; lexicon.json is editable
  alerts.py     owner messages in Hindi / Marathi / English, yes/no reply parsing
  route.py      technician route: exact best-risk-within-hours, shortest order, ETAs
  agent.py      4 tools (none can control a machine), Claude tool loop or fixed-order rules, all logged
  sim.py        3 factories x 4 machines replaying real clips
web/         React + Vite + TypeScript, react-three-fiber (instanced meshes, ~10-14 draw calls)
data/        download.py (Zenodo range requests), sim/ (cluster, mock parts, voice notes), raw/ (ignored)
docs/        media/, runs/ (every evaluation, ASR and story record the README cites)
tests/       pytest
```

## Results

Every number below and on the dashboard comes from one evaluation record; regenerate both with
`make train && python -m edge.report`. Clip-level AUC/pAUC (pAUC = DCASE
definition, FPR <= 0.1). "false alarms / faults caught" use each machine's own alarm level.

From `docs/runs/eval_20261115T085722Z.json` (code sha256 a1b9d12adbbf, hashed over the model code
as it was then). The code has moved and changed since (`edge/runtime.py` also returns per-band
deviations and scores with 4 threads; files moved from `dhadkan/` into `edge/` and `server/`);
none of it changes how a clip is scored, and `tests/test_thresholds.py` checks the shipped
thresholds against this record.

| type | condition | AUC | pAUC | normal + anomalous | false alarms | faults caught |
|---|---|---|---|---|---|---|
| pump | -6dB (stress test) | 0.945 | 0.650 | 20 + 20 | 95% | 100% |
| pump | 6dB | 0.727 | 0.500 | 32 + 40 | 9% | 65% |
| fan | 6dB | 0.991 | 0.906 | 32 + 40 | 6% | 100% |
| gearbox | source | 0.659 | 0.058 | 60 + 60 | 10% | 12% |
| gearbox | target (stress test) | 0.579 | 0.067 | 30 + 30 | 10% | 20% |

| recorded unit | AUC | pAUC |
|---|---|---|
| pump/id_00 | 1.000 | 1.000 |
| pump/id_02 | 0.828 | 0.206 |
| fan/id_00 | 0.991 | 0.906 |
| fan/id_02 | 1.000 | 1.000 |
| gearbox/section_00 | 0.570 | 0.100 |
| gearbox/section_01 | 0.747 | 0.225 |
| gearbox/section_02 | 0.635 | 0.000 |

Registry, synthetic onset 0.25 (alarm / registry), registry false flags on normal clips:

| case | from | to | alarm | registry | false flags |
|---|---|---|---|---|---|
| same model | pump/id_00 | pump/id_00 | 100% | 100% | 0% |
| other unit | pump/id_00 | pump/id_02 | 0% | 6% | 0% |
| other unit | pump/id_02 | pump/id_00 | 94% | 25% | 0% |
| same model | pump/id_02 | pump/id_02 | 0% | 100% | 50% |
| same model | fan/id_00 | fan/id_00 | 12% | 75% | 0% |
| other unit | fan/id_00 | fan/id_02 | 0% | 0% | 25% |
| other unit | fan/id_02 | fan/id_00 | 0% | 38% | 6% |
| same model | fan/id_02 | fan/id_02 | 6% | 31% | 31% |
| same model | gearbox/section_00 | gearbox/section_00 | 0% | 31% | 25% |
| other unit | gearbox/section_00 | gearbox/section_01 | 0% | 0% | 0% |
| other unit | gearbox/section_00 | gearbox/section_02 | 30% | 35% | 30% |
| other unit | gearbox/section_01 | gearbox/section_00 | 15% | 10% | 20% |
| same model | gearbox/section_01 | gearbox/section_01 | 0% | 0% | 10% |
| other unit | gearbox/section_01 | gearbox/section_02 | 30% | 0% | 0% |
| other unit | gearbox/section_02 | gearbox/section_00 | 15% | 5% | 10% |
| other unit | gearbox/section_02 | gearbox/section_01 | 0% | 0% | 5% |
| same model | gearbox/section_02 | gearbox/section_02 | 25% | 6% | 20% |

What these numbers say:

- **Fan works; pump is uneven; gearbox is weak.** The pooled pump AUC (0.73) is lower than
  either pump unit's because the two units produce scores on different scales; that is why every
  machine gets its own alarm level. Gearbox (MIMII DUE) is near chance in places (section_02
  pAUC 0.000). We show it rather than drop it.
- **Noisy shed.** The same pump under louder factory noise (MIMII -6 dB) still ranks faults well
  (AUC 0.945), but with the alarm level set in the quieter recording, 95% of normal readings raise
  an alarm. Alarm levels have to be re-set after installing in a new shed.
- **Speed or load change.** The gearbox under changed operating conditions (DUE target domain)
  drops to AUC 0.579 and catches 20% of faults.
- **The registry helps only between machines of the same model.** On held-out clips of the same
  blower model, a 0.25-strength onset is flagged by the registry 75% of the time against 12% by the
  machine's own alarm, with 0/16 false flags. Across different units it mostly fails, and some
  same-model cases have high false-flag rates (pump id_02: 50%). "Same model" is approximated by
  disjoint clips of one recorded unit; we have no recordings of two identical machines.
- **Voice notes.** On our synthetic notes, Whisper-small labelled the Hindi note correctly in 3 of
  3 runs and the Marathi note in 1 of 3 (`docs/runs/asr_*.json`). Typed notes and the Claude fallback
  cover misses.

## Assumptions and limits

- **Data.** MIMII 6 dB pump and fan (units id_00, id_02), MIMII -6 dB pump id_00, MIMII DUE gearbox
  (sections 00-02). A small subset: 34 training clips per MIMII unit, 50 per gearbox section; test
  sets are 16-20 normal + 20 anomalous clips per unit, so each AUC has wide error bars.
- **Alarm level** = 95th percentile of each machine's held-out normal readings (so ~5% of healthy
  readings cross it by design). **Registry flag** = above the highest similarity the machine showed
  while healthy (99th percentile of its held-out normals), in 2 of the last 3 readings.
- **Synthetic fault onset.** MIMII has no recordings of a fault developing. Onset is an abnormal
  recording mixed into a normal one with power-preserving weights. It is labelled SYNTHETIC
  everywhere it appears.
- **Folk-sound lexicon weights** (`dhadkan/lexicon.json`) are our assumptions, not measurements.
  A label from an owner is a weak label, not an inspection. MIMII does not say which fault each
  abnormal clip is, so we cannot score the fault-class mapping.
- **Simulation.** Factory names, owners, phone numbers, locations, service hours (pump 1 h, fan
  0.75 h, gearbox 1.5 h), road speed (22 km/h) and the technician's 09:00 start are invented.
  The looms and the flour mill replay MIMII DUE gearbox recordings, standing in for their gear
  drives; the 3D shapes are only pictures. Voice notes are macOS
  text-to-speech, not real owners. Dealer parts and prices are mock data.
- **Phone.** We export a 256 KB `.tflite` (dynamic-range int8 weights) and check it scores within
  0.2% of the Keras model; the log-mel front end to re-implement on Android is in
  `edge/models/frontend.json`. We did not build the Android app.
- **Agent.** It has four tools (risk ranking, route, owner message, parts quote) and no tool that
  can control a machine. Owner text only comes from reviewed templates. It does not repeat a
  question while one is open or a visit is already approved. Model: `claude-opus-5-5` with
  server-side fallback (`fallbacks: "default"`).

## Data credits

- Purohit et al., "MIMII Dataset: Sound Dataset for Malfunctioning Industrial Machine
  Investigation and Inspection", DCASE 2019. https://zenodo.org/records/3384388 (CC BY-SA 4.0)
- Tanabe et al., "MIMII DUE: Sound Dataset for Malfunctioning Industrial Machine Investigation
  and Inspection with Domain Shifts", 2021. https://zenodo.org/records/4740355 (CC BY-SA 4.0)

The clips in `docs/media/` are unmodified channel-0 extracts from MIMII and keep its CC BY-SA 4.0 license.
