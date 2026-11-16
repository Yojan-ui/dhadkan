# WhatsApp message templates (submit in Meta Business Manager)

Meta delivers a business-initiated message (an alert the owner did not just ask for) only as an
**approved template**. Inside the 24-hour window after the owner's last message, Dhadkan sends plain
text with three reply buttons instead (`server/notify/whatsapp.py`, `interactive_payload`).

Templates are used when `WA_TEMPLATES=1` is set in `.env`, which you set after Meta approves them. The
whole alert text goes in as variable `{{1}}`, built from `server/faults.yaml` by
`server/diagnosis.py`. Meta may ask for some fixed text around the variable; the bodies below have it.

| name | sent for | category |
|---|---|---|
| `alert_watch` | severity watch (normally only inside the daily digest) | Utility |
| `alert_repair` | repair soon | Utility |
| `alert_stop` | stop now / beyond repair | Utility |
| `daily_digest` | the evening list of watch-level machines | Utility |

Languages to submit for each template: `hi`, `mr`, `gu`, `en`. Hindi in Roman script goes out under `en`
(`META_LANG` in `server/notify/whatsapp.py`).

**Body** (one per language; the variable carries the full alert in that language):

- hi: `मशीन अलर्ट: {{1}}`
- mr: `मशीन सूचना: {{1}}`
- gu: `મશીન એલર્ટ: {{1}}`
- en: `Machine alert: {{1}}`

**Quick-reply buttons** (alerts, not the digest): ids are matched by text, so keep these titles.

| language | button 1 (book) | button 2 (later) | button 3 (details) |
|---|---|---|---|
| hi | मिस्त्री बुलाओ | बाद में | पूरी जानकारी |
| hi (Roman, under en) | Mechanic bulao | Baad mein | Details |
| mr | मिस्त्री बोलवा | उद्या | माहिती |
| gu | મિસ્ત્રી બોલાવો | કાલે | માહિતી |
| en | Send mechanic | Later | Details |

Only button 1 (or a reply of `1`) books a mechanic. No button, reply or template can stop a machine.

**Not built: spoken alerts.** A voice version of each alert needs a text-to-speech service for
Hindi, Marathi and Gujarati (for example AI4Bharat Indic TTS, or a cloud TTS). None ships with this
repo, and none was evaluated.
