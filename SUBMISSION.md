# Dhadkan: Tech4Bharat submission

Problem statement 4.3: machine failure prediction for small factories.

## What to submit

- **Repository:** https://github.com/Yojan-ui/dhadkan (branch `main`)
- **Pitch deck:** the `sih-deck` deck
- **Screenshot:** `docs/screenshots/submission/factory-screen.jpg`
- **Architecture diagram:** README → Architecture
- **Demo video / GIF:** `docs/media/demo.gif` (60 seconds of demo mode)

## One-line pitch

Dhadkan uses a phone to hear machine failures coming, tells the owner on WhatsApp in their own
language, gets the right repair booked only after the owner says yes, and checks that the repair worked.

## How to run

```bash
npm run setup     # first time only: Python venv + web app
cp .env.example .env   # then fill in TWILIO_SID, TWILIO_TOKEN, TWILIO_WA_FROM, OWNER_PHONE, ANTHROPIC_API_KEY
npm run demo      # backend + web app; the NASA IMS bearing failure replays as Loom 1
```

Set `DHADKAN_DRY_RUN=1` in `.env` to rehearse without sending real WhatsApp messages.

## Features

- Real run-to-failure data: the NASA IMS bearing failure replays as a loom motor, the fault grows live.
- Digital twin and command centre dashboards (main factory screen, plus `?view=cc`, `?view=owner`, `?view=mechanic`).
- Diagnosis from sound: fault name, severity, days to failure, part number and cost in rupees.
- Owner's agent (observe, diagnose, plan, ask owner, act, verify) with Claude tool use, rules offline.
- Real WhatsApp alerts through Twilio, in Hindi and other Indian languages; owner replies 1 / 2 / 3.
- Anything that costs money waits for the owner's "1 = Haan". No tool can stop a machine.
- Command centre maintenance agent on WhatsApp: booking question with a ref, rate-capped, only `.env` numbers.
- 315 automated tests (`npm test`).

## Demo steps (about 3 minutes)

1. Run `npm run demo` and open the web app it prints.
2. Show the factory floor: all machines teal (healthy).
3. Watch Loom 1 turn amber, then red as the real bearing fault grows.
4. Show the agent's steps: diagnosis, part, cost in rupees, ask owner.
5. Show the WhatsApp alert arriving on the owner's phone in Hindi.
6. Reply `1` on WhatsApp: the mechanic is booked; the Messages timeline in the command centre updates.
7. Close on the verify step: the sound after repair is compared with the healthy baseline.
