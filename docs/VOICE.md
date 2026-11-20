# Voice: alerts as voice notes, questions by voice

Small-factory owners would rather listen than read. Dhadkan's templates stay the source of truth: every voice
note is read from the **same template text** as the WhatsApp message, never from free LLM text.

## What happens

- **Owner alerts and the daily digest** go out with a short WhatsApp voice note (OGG/Opus, at most 30 s) in
  the owner's language (`server/owner_alerts.py` → `server/voice.py`). When the note would be longer than 30 s,
  the reply options ("1 = …") are dropped first (the text carries them), then other sentences from the end.
  The first sentence (what is wrong) and the disclaimer (typical prices, confirm with the dealer) always stay.
  There is no weekly digest in the code yet, only the daily one.
- **Cache**: one file per (template, values, language, engine), `data/tts/cache/<sha>.ogg`. A repeated alert
  costs a file read (~1 ms).
- **hi_latn owners** hear the Devanagari Hindi rendering of the same template: Hinglish text read aloud by a
  Hindi voice sounds wrong.
- **Questions by voice**: an owner's voice note is transcribed by the existing faster-whisper path
  (`server/folk.py`). Whisper detects the language when no alert is open. The question then goes to the
  owner commands or the planner, and the reply comes back as text plus a template voice note. The audit
  event is `owner_voice_question`, and it records `reply_source`.
- **Granite-written replies** to an owner's chat are used only when they pass the number grounding, the
  Guardian, and a language check (`detect_lang(reply)` equals the owner's language). Otherwise the template
  reply is used. Every reply logs `reply_source` (granite / template, with the reason).
  `DHADKAN_GRANITE_REPLIES=0` always uses the template. The voice is always the template.
- **Owner screen** (`?view=owner`): a "सुनो / Suno / ऐका / સાંભળો / Listen" button on each alert plays
  `GET /api/voice/machine/{id}.ogg?lang=`.

## Engines (measured on this laptop: docs/runs/tts_20261120T053436Z.json)

Quality is the round-trip character error rate: synthesize the template text, transcribe it with
faster-whisper small, and compare. Part of that error is Whisper's own, especially in Gujarati. Speed is the
real-time factor (synthesis time ÷ audio length), measured on CPU with 8 threads.

| Language | Picked | CER | RTF | Others measured |
|---|---|---|---|---|
| Hindi | Piper `hi_IN-priyamvada` (sherpa-onnx) | 0.19 | 0.08 | MMS-hin 0.19 / 0.44; Piper rohan 0.27; pratham 0.46 |
| Marathi | Meta MMS-TTS `mar` | 0.33 | 0.41 | none: no other CPU engine covers Marathi here |
| Gujarati | Meta MMS-TTS `guj` | 0.61 | 0.42 | none. **A native speaker should listen before relying on it** |
| English | Meta MMS-TTS `eng` | 0.16 | 0.42 | |
| Tamil | Meta MMS-TTS `tam` | not evaluated | | There is no Tamil alert template yet |

The order of evaluation:
1. **AI4Bharat IndicF5 and Indic Parler-TTS**: not evaluated. Both are gated on Hugging Face. To include
   them, accept their terms, set `HF_TOKEN` and run `.\dev.ps1 tts-eval` again.
2. **Edge-friendly engines**: Piper (ONNX; Hindi only) and MMS-TTS (VITS on CPU torch).
3. **Pre-recorded audio**: this is the cache above. A cached note is a file read.

Windows SAPI was also checked; only English voices are installed.

Licences: the **MMS-TTS models are CC-BY-NC-4.0 (non-commercial)**, so replace them before selling. Piper's
Hindi voices are free to use.

MMS-TTS has almost no digits in its vocabulary. Numbers are spelled out for it: digit by digit in
Hindi/Marathi/Gujarati/Tamil, as Indian-English words in English. Piper reads numbers itself.

## Speech to text

IBM Granite Speech was checked: it supports English, French, German, Spanish and Portuguese, not Hindi or
Marathi. So the existing faster-whisper path stays.

## Setup (optional; without it alerts go out as text only)

```powershell
.\dev.ps1 voice-models   # CPU torch, sherpa-onnx, transformers, then the models into data/tts/models
.\dev.ps1 tts-eval       # measure again -> docs/runs/tts_<UTC>.json
```

`fetch_voice_models` downloads with Windows' certificate store (urllib), because this machine's `requests`
fails TLS verification. Switches (`.env`): `DHADKAN_VOICE=0` turns voice off; `DHADKAN_TTS_HI=mms-hi` (and so
on) overrides the engine for a language; `DHADKAN_GRANITE_REPLIES=0` always uses the template reply.
