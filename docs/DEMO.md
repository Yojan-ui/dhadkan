# Dhadkan in 90 seconds

**Before you start:** `make demo` (opens http://127.0.0.1:8600 and starts the replay). For a clean
take, press **D** twice (off, on) about **10 seconds before your 90 s begin**, while you say who you
are. The replay first records 14 simulated days (~8 s, a progress bar shows it), then plays live: one
real 10 s recording per machine every 10 s.

Times below are the **live replay clock** shown next to the switch (`0:40 · fault`). They come from the
replay script in `server/app.py` and the logged run `docs/runs/story_20261115T154737Z.json`.

| clock | do | say |
|---|---|---|
| **0:00** | Nothing. Let the map breathe. | "Three small factories near Pune, twelve machines. Each one is a phone listening to it. The ring is how close a machine is to *its own* alarm level; the shake is what it sounds like this second." |
| **0:05** | Press **1** (fly into Shinde Engineering), then **]** three times to Blower 1. | "This blower has been getting worse for days." |
| **0:10** | Press **Space** (play 14 days). Watch the ring and the waterfall rewind and replay. | "This is its last two weeks. Healthy until day 6, then a sound starts creeping in. Dhadkan flagged it here" (point at **first warning**) "five days before it broke down" (point at **breaks down**). |
| **0:25** | Press **N** (back to live). Point at **Days to fault level** and **What changed in the sound**; click **Before · 3 s**, then **Now · 3 s**. | "It gives the owner a range, not a promise, and lets them *hear* the difference: this low rumble carries about six times the power it did on day one. The owner's own word for it was *khat-khat*." |
| **0:40** | (the replay switches A-F1 to the recorded fault) | "And now it has actually failed: this is the real recording of the broken machine." |
| **0:45** | Press **0** (whole cluster). | "The owner sends a voice note in Hindi. It's transcribed on this laptop, no cloud, and *khat-khat* becomes a bearing label." |
| **1:10** | Watch the arcs leave Blower 1. | "Only 64 numbers leave the factory, never audio. Every blower in the other sheds is checked against it: these labels are the real similarity scores." |
| **1:20** | Watch B-F1 in Gupta Pumps pulse; the route draws itself. | "Same model of blower, 22 km away, starts to sound the same. Its own alarm hasn't fired; the registry caught it first. The agent plans Ramesh's day around the rupees at risk." |
| **1:25** | Point at the Marathi SMS at the bottom; click **हो Ho**. | "The owner gets one plain message in Marathi and answers yes or no. Nothing ever switches a machine off." |
| **1:28** | Click **Savings report (PDF)** in the breadcrumb (Gupta Pumps). | "And a one-page savings report for the owner. Today the costs are our assumptions, printed on the page; with the factory's own numbers it is something to show a bank or a buyer." |
| **1:30** | Stop. | |

**If something goes wrong**

- No 3D (old laptop, no WebGL): shrink the window below 768 px; the phone layout shows the same
  rings and messages without three.js.
- Replay stuck or you want to restart: press **D** twice. It always rebuilds the same 14 days.
- A judge asks "is that real?": open **Evaluation** (press **E**). Every score there, including the weak
  gearbox and the noisy-shed failure, comes from `docs/runs/eval_20261115T085722Z.json`. The growing
  faults are synthetic mixes of real recordings, and the costs are assumptions; both are labelled on
  screen.

**Keys:** `?` all shortcuts · `1 2 3` sheds · `0` cluster · `[ ]` machines · `← →` time machine
(Shift = a day) · `Space` play · `N` now · `D` replay · `Esc` back.

Screenshots of the same run: [after](screenshots/after) · the dashboard before this round:
[before](screenshots/before).
