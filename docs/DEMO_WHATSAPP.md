# WhatsApp loops: investigation, the mechanic's job, the owner's commands (Windows)

What happens on WhatsApp after an alarm, both sides, and how to show it without any phone.

## The loops

**Investigation.** When the diagnosis is unsure, the planner (`request_measurement`) sends the owner one
precise instruction in their language:

| kind | message (hi_latn) | the answer | what it does |
|---|---|---|---|
| recording | "Phone ko Blower 1 ki motor ke paas rakh ke 10 sec ki awaaz record karo aur yahan bhejo." | a voice note / audio file | scored against this machine's own learned normal, like a reading |
| hand check | "Blower 1 band hone par motor ka end chhoo ke dekho: garam hai? 1 = haan, 2 = nahi" | 1 / 2 | recorded as a hand-check fact |
| photo | "Blower 1 ki belt ki ek saaf photo bhejo, jo theek se dikhe." | a photo | stored and attached to the case (no image model: it is for the mechanic) |

The case carries a confidence (0 to 1, starting from the health assessment's). An answer that supports a fault
moves it 60% of the way to 1 (0.55 → 0.82); one that speaks against it halves it; a photo leaves it. These
are assumptions and are written in the trace. Then an `investigation_result` event goes to the planner, and
the trace shows **asked → answered → confidence 0.55 → 0.82**.

**The mechanic.** Only after the owner's logged yes (WhatsApp "1", or the command centre), the mechanic gets
the job: machine, factory and area, fault, the part to bring (from faults.yaml), the slot, and
`1 = Aaunga, 2 = Nahi aa sakta, 3 = Doosra samay chahiye`.

| the mechanic sends | what happens |
|---|---|
| 1 | accepted; they are told what to send next |
| 2 | declined: re-plan with the next candidate. The owner is asked again **only if the new visit costs more** than what they approved |
| 3 | same mechanic, another day |
| nothing for `DHADKAN_MECHANIC_REPLY_HOURS` (4 h) | a no-show for planning: re-plan |
| "raste mein 20 min" | the owner is told: on the way, about 20 min |
| "ho gaya: …" without a photo | refused: "Pehle purane part ki photo bhejiye" |
| a photo, then "ho gaya: bearing badla" | repair reported → the owner re-records → verification against baseline v1 → a signed repair record (with the photo's sha256 and the note) |

**The owner's commands**, answered from stored facts in the script the owner typed: `status`, `kitna kharcha`,
`kab aayega mistri`. **`band karo`** never calls a tool: the reply explains that only the owner can stop a
machine (Dhadkan cannot stop or start anything) and the request is logged.

## Without phones: the simulated phones

```powershell
$env:DHADKAN_DRY_RUN = "1"; .\dev.ps1 serve
```

Open **http://127.0.0.1:8600/?view=phones**: the owner's and the mechanic's WhatsApp side by side, with quick
replies, a text box, a photo button and (owner) a recording button. Every message goes through the same code
path as the Meta or Twilio webhook. In a dry run, numbers that are not in `.env` get demo numbers with
country code **+0**, which no network routes; with a real provider configured, the simulated phones are off.

A 3-minute run on a real fan (docs/DEMO_LIVE.md for the recorder):

1. Learn the fan's normal with the recorder, then tape the coin on: the brain opens a case.
2. Owner phone: the booking question arrives ("… Reply 1 to book"); tap **1**.
3. Mechanic phone: the job arrives; tap **1**, then **raste mein 20 min** (the owner phone shows it).
4. Mechanic: **ho gaya: bearing badla**: refused, a photo is needed. Send a photo, then **ho gaya: …** again.
5. Take the coin off; the recorder sends normal clips; the owner phone gets "back to its original normal",
   and `/api/records` has the signed record.
6. Owner: **band karo** → the explanation, no action.

## With real phones

Set in `.env`: `DHADKAN_PHONE_A` (owner), `DHADKAN_MECHANIC_A` (the factory's own mechanic) or
`DHADKAN_TECH_T2` (any technician by id), the provider keys (README: Twilio sandbox or Meta), and point the
provider's webhook at `/webhooks/twilio` or `/webhooks/whatsapp` (`.\dev.ps1 tunnel` gives a public HTTPS
URL). `DHADKAN_AGENT_WHATSAPP=0` keeps the agent's messages internal again.

Not done here: the fixed plan's own messages (booking question, "booked", "after the repair") are still in
English; the planner's and the mechanic's messages are in the person's language.
