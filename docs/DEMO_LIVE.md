# Live demo: a real fan, a real fault, detected live (Windows)

A phone (or the laptop microphone) listens to a real table fan. Dhadkan learns the fan's normal sound in
about two minutes, then you tape a coin to one blade (imbalance) or let a card tick against the blade (a
rubbing, bearing-like noise). The score jumps, the health rule raises an alarm, the planner plans, and the
owner gets a WhatsApp message (or the labelled dry run). Every screen says **LIVE**: this machine is not
simulated, and its normal was learned in a **short learning period, demo**.

## What to bring

- Any table or pedestal fan with a front guard (a cheap one is fine; it must have a guard).
- A phone with Chrome or Edge (Android) or Safari (iPhone), charged. Or only the laptop and its microphone.
- One coin (a ₹1 or ₹2 coin) and strong tape (electrical or duct tape). Optionally a strip of stiff card.
- The laptop with this repo, on mains power. A phone stand or a cup to hold the phone in the same place.

**Safety.** Switch the fan **off and unplugged** before you tape anything to a blade. Tape the coin flat near
the hub (not the tip), with two crossed strips, and run the fan on its **lowest speed with the guard on**.
Keep the audience a metre away. A coin that comes loose leaves through the guard slowly at low speed; at high
speed it does not. The card for the rubbing noise is taped to the **guard**, so a blade tip ticks against it;
nobody holds anything near the blades. Dhadkan never switches the fan on or off: you do.

## Once, the day before

```powershell
.\dev.ps1 setup
.\dev.ps1 test; .\dev.ps1 lint
winget install --id Cloudflare.cloudflared      # only for a phone (HTTPS); the laptop mic needs nothing
```

WhatsApp is optional: with keys in `.env` the alert goes out for real (README, Twilio sandbox or Meta);
without them it is a **dry run** shown in the Log drawer. The owner number for factory A is
`DHADKAN_PHONE_A` in `.env` (B: `DHADKAN_PHONE_B`, ...).

Granite (the planner) is optional too: with Ollama running it plans the alarm (minutes on a CPU-only laptop,
see docs/DEMO_GRANITE.md); without it the rules plan it at once. Either way the trace shows which one did.

## Ten minutes before

| window | command | wait for |
|---|---|---|
| 1. app | `.\dev.ps1 serve` (or `demo-ims`) | http://127.0.0.1:8600 opens |
| 2. phone only | `.\dev.ps1 tunnel` | `Recorder on the phone (HTTPS): https://….trycloudflare.com/?view=record` and a QR code |

- **Laptop microphone:** open **http://localhost:8600/?view=record** in the laptop's browser (localhost is a
  secure context, so no tunnel). Allow the microphone when asked.
- **Phone:** scan the QR code from window 2. Allow the microphone. In the browser menu, **Add to Home
  screen** installs the recorder (it opens full screen next time).
- Second screen for the audience: the Digital Twin at http://127.0.0.1:8600 and the command centre at
  `?view=cc` → AI Agents → Planner trace.

## The stage script (about 6 minutes)

| time | do | say / what they see |
|---|---|---|
| 0:00 | Fan on, lowest speed. Phone on its stand about 20 cm from the motor housing, same place throughout. | "This is a real fan, not a recording." |
| 0:15 | Recorder: factory **A**, machine **Blower 1** (or **+ Register a new machine**: "Table fan", fan, 0.1 HP). **Quick learn** ticked. Tap **Learn normal**. | The level meter moves. "It listens for two minutes and learns this fan's own normal." |
| 0:15–2:15 | Let it learn. Talk over it: the phone sends each 10 s clip to this laptop, which keeps only a summary of it (never the audio), and why each machine gets its own normal. | Progress `1/12 … 12/12 clips` and **short learning period, demo**. The twin shows the machine with a **LIVE** badge, "learning". |
| 2:15 | When it says **Monitoring**, tap **Stop**, then **Monitor** (a clip every 10 s). | Score around 0 to 0.5 (1.0 = this fan's alarm level). The twin's ring stays calm. |
| 2:45 | **Switch the fan off.** Tape the coin to one blade near the hub. Fan back on, lowest speed. Tap **Mark: fault added**. | "Same fan, one coin heavier on one side: an unbalanced rotor, the commonest fan fault in a flour mill." |
| 3:00–3:30 | Wait two or three clips. | The score jumps above 1.0 on the phone; on the twin the ring turns red and the risk list puts the fan first. After two of three clips over its alarm level (and a median at least 2×), health goes to **inspect** or higher: that is the alarm. |
| 3:30 | Command centre → Planner trace, newest alarm step. | The alarm event, the facts fetched first, the knowledge-base chunks (imbalance card), the Guardian's checks; Granite or the rules plan the visit and ask the owner. |
| 4:00 | The owner's phone (or the Log drawer in a dry run). | The WhatsApp question "… 1 = Haan, 2 = Baad mein". Reply **1**: the planner books the mechanic (only after that logged yes). |
| 4:30 | **Switch the fan off**, remove the coin, fan on. Tap **Mark: fault removed**. | The score falls back under 1.0 within a clip or two. |
| 5:00 | Stop. | "Two minutes to learn, under a minute to notice, and nothing ever switched the fan off but me." |

For the rubbing variant, instead of the coin: switch off, tape the card to the guard so a blade tip just
touches it, switch on at the lowest speed. It is louder and broadband; it is detected faster.

## The expected score jump, measured

Measure it on **your** fan before the demo, with the recorder in **Monitor** mode:

1. Learn normal (quick learn), then **Monitor** for **3 minutes** of normal running.
2. Switch off, tape the coin, switch on, tap **Mark: fault added**, and record **1 minute**.
3. Then, in the repo:

```powershell
.\dev.ps1 live-trial A-F1 --note "table fan, coin on one blade, lowest speed"
```

It writes **docs/runs/live_<UTC>.json** and prints:

- **normal:** readings, median / 95th-percentile / max risk, and **false alarms** = times the brain's own
  health rule (server/health.assess) would have raised an alarm during normal running;
- **fault:** median and max risk, and the **score jump** = fault median minus normal median, in multiples of
  the fan's alarm level;
- **detection delay:** seconds from your mark to the end of the first clip over the alarm level, and to the
  end of the clip that raised the alarm (upload and processing add a few seconds).

Nothing is written without live readings and a **fault added** mark. Quote only those numbers on stage.
Until you have run it on your fan, there is **no measured number** in this repo: the synthetic clips in
`tests/test_live_recorder.py` check the plumbing only, not detection on a real fan.

What to expect from the rule itself: with a clip every 10 s, an alarm needs two of three clips over the
alarm level, so at best about 20 to 30 s after the coin goes on. A quick-learned normal is 12 clips, so
health confidence stays "medium" ("its normal was learned from only 12 readings"); that is honest for a demo.

## If something goes wrong

- **"Microphone blocked":** allow the microphone for the site (lock icon in the address bar), reload.
- **"The microphone needs HTTPS on a phone":** open the tunnel's https link, not the laptop's IP address.
- **`.\dev.ps1 tunnel` says neither tool is installed:** `winget install --id Cloudflare.cloudflared`, or use
  the laptop microphone on `http://localhost:8600/?view=record`.
- **No network on stage:** the recorder keeps clips in the phone (IndexedDB) and sends them, oldest first,
  when the network is back; a clip that arrives twice is counted once.
- **The score jumps while still normal (people talking, a door):** keep the phone close to the motor and
  the room quiet; one loud clip is not an alarm (two of three are needed).
- **Start over:** tap **Learn normal** again: the machine's earlier readings are archived and it learns again.
