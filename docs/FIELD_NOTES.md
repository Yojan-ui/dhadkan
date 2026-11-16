# Field notes (template)

One section per factory visit. Field recordings and results stay on the recording computer
(`runs/field/`, git-ignored). Copy only the numbers you are allowed to share into this file. Never
paste an owner's name, phone number or audio here without their written consent.

---

## Visit: <date>, <town>, <factory type>

**Consent:** owner agreed to recordings (yes/no), to sharing numbers (yes/no).

**Phone and placement:** model, Android version, where the phone sat (distance, surface), shed noise
(other looms running? generator?).

**Machines recorded**

| machine | model used (`--type`) | baseline recordings (days, total minutes) | new recordings |
|---|---|---|---|
| | | | |

**Scores** (`python -m scripts.score_recording ...`, file `runs/field/<run_id>.json`)

| machine | reading | risk (1.0 = alarm) | loudness z | noisy shed |
|---|---|---|---|---|
| | | | | |

**What the owner heard and said** (their words, their language):

**What the mechanic found** (opened? which part? photo reference):

**Did Dhadkan get it right?** flagged and faulty / flagged and fine (false alarm) / missed /
not flagged and fine.

**Repair and re-record:** part fitted, re-record outcome (fixed / partly / failed), record SHA-256.

**Problems with the app or the method:**
