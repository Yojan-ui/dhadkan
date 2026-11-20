"""Voice notes: owner alerts and digests also go out as a short WhatsApp voice note, read aloud from the SAME
template text (never from free LLM text). Granite is not tested on Hindi / Marathi / Gujarati speech, so the
templates stay the source of truth.

    v = voice.note("alert_repair", {"machine": "A-G1", "body": text}, "hi", text)
    -> {"path": ".../cache/<key>.ogg", "seconds": 18.4, "engine": "piper-hi-pratham", "cached": False}

Engines (chosen by scripts/tts_eval.py on this laptop, docs/runs/tts_*.json; override per language with
DHADKAN_TTS_<LANG>, e.g. DHADKAN_TTS_HI=mms-hi, or switch voice notes off with DHADKAN_VOICE=0):
  * Piper voices through sherpa-onnx (ONNX, CPU): Hindi;
  * Meta MMS-TTS through transformers (VITS, CPU torch): Marathi, Gujarati, Tamil, English. Its license is
    CC-BY-NC-4.0 (non-commercial): fine for a pilot, to be replaced before selling (AI4Bharat models are
    the candidates; they are gated and need an account, see the evaluation).
hi_latn owners get the Hindi (Devanagari) rendering of the same template, which is what a Hindi voice reads.

A voice note is at most MAX_SECONDS: when the template text reads longer, trailing sentences are dropped
(the text message carries everything). Cached per (template, values, language, engine): a repeated alert is
a file read. Any failure returns None and the text alert still goes out.
"""

import hashlib
import io
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np

from .config import ROOT
from .notify import env

CACHE = ROOT / "data" / "tts" / "cache"
MODELS = ROOT / "data" / "tts" / "models"
MAX_SECONDS = 30.0
GAP_S = 0.25  # pause between sentences
VERSION = 2  # bump when the spoken form of the templates changes, so old cached notes are not reused
VOICE_LANG = {"hi_latn": "hi"}  # the Roman-script owner hears the Hindi rendering of the same template
# chosen per language from the evaluation on this laptop (docs/runs/tts_*.json); see CHOICE_NOTE
# Picked in docs/runs/tts_20261120T053436Z.json (this laptop, CPU): Hindi Piper "priyamvada" (round-trip CER
# 0.19, RTF 0.08; MMS-hin was as clear, 5x slower and non-commercial); Marathi / Gujarati / English: MMS-TTS,
# the only engines that cover them here (CER 0.33 / 0.61 / 0.16, RTF ~0.42). Gujarati's CER is high, partly
# faster-whisper's own Gujarati error: a native speaker should listen before relying on it. Tamil: MMS-tam,
# not evaluated (there is no Tamil alert template yet).
CHOICE = {"hi": "piper-hi-priyamvada", "mr": "mms-mr", "gu": "mms-gu", "ta": "mms-ta", "en": "mms-en"}
CHOICE_NOTE = "docs/runs/tts_20261120T053436Z.json"
_engines: dict[str, Any] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return env.get("DHADKAN_VOICE") != "0"


def engine_for(lang: str) -> str | None:
    lang = VOICE_LANG.get(lang, lang)
    return env.get(f"DHADKAN_TTS_{lang.upper()}") or CHOICE.get(lang)


def cache_key(template: str, values: dict[str, Any], lang: str, engine: str) -> str:
    raw = json.dumps(
        [VERSION, template, values, lang, engine], ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def cache_path(template: str, values: dict[str, Any], lang: str, engine: str) -> Path:
    return CACHE / f"{cache_key(template, values, lang, engine)}.ogg"


# ---------------------------------------------------------------------- speaking the template text
SPOKEN = {  # symbols the voices do not read: the same words, spelled out (still the template's own content)
    "hi": [("~", "लगभग "), ("₹", " रुपये "), ("–", ", "), ("·", ". "), ("=", " ")],
    "mr": [("~", "सुमारे "), ("₹", " रुपये "), ("–", ", "), ("·", ". "), ("=", " ")],
    "gu": [("~", "લગભગ "), ("₹", " રૂપિયા "), ("–", ", "), ("·", ". "), ("=", " ")],
    "ta": [("~", "சுமார் "), ("₹", " ரூபாய் "), ("–", ", "), ("·", ". "), ("=", " ")],
    "en": [("~", "about "), ("₹", " rupees "), ("–", ", "), ("·", ". "), ("=", " ")],
}


def spoken(text: str, lang: str) -> str:
    """The template text as it is read aloud: symbols as words, '1,200-2,400' as '1200 to 2400'."""
    to = {"hi": " से ", "mr": " ते ", "gu": " થી ", "ta": " முதல் ", "en": " to "}.get(lang, " - ")
    t = re.sub(r"(\d),(\d)", r"\1\2", text)
    t = re.sub(
        r"₹\s*(\d+(?:\s*[-–]\s*\d+)?)", r"\1 ₹", t
    )  # the currency word after the amount, as it is said
    t = re.sub(r"(\d)\s*-\s*(\d)", lambda m: f"{m.group(1)}{to}{m.group(2)}", t)
    for a, b in SPOKEN.get(lang, SPOKEN["en"]):
        t = t.replace(a, b)
    t = re.sub(r"\(([^)]*)\)", r", \1.", t)
    return re.sub(r"\s+", " ", t).strip()


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.।!?])\s+", text) if s.strip()]


# MMS-TTS has (almost) no digits in its vocabulary: a number would be skipped, so it is read digit by digit
# with these words (Piper reads numbers itself, through espeak-ng). English gets proper number words.
DIGITS = {
    "hi": "शून्य एक दो तीन चार पाँच छह सात आठ नौ",
    "mr": "शून्य एक दोन तीन चार पाच सहा सात आठ नऊ",
    "gu": "શૂન્ય એક બે ત્રણ ચાર પાંચ છ સાત આઠ નવ",
    "ta": "பூஜ்ஜியம் ஒன்று இரண்டு மூன்று நான்கு ஐந்து ஆறு ஏழு எட்டு ஒன்பது",
}
ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
TENS = ["_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def english_number(n: int) -> str:
    """Indian grouping, as Indian English says it: 1,56,960 -> one lakh fifty six thousand nine hundred sixty."""
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    for size, word in ((10_000_000, "crore"), (100_000, "lakh"), (1000, "thousand"), (100, "hundred")):
        if n >= size:
            rest = n % size
            return f"{english_number(n // size)} {word}" + (f" {english_number(rest)}" if rest else "")
    return str(n)


def speakable(text: str, lang: str, engine: str) -> str:
    """Numbers in a form this engine will say: Piper reads digits itself; MMS gets them as words."""
    if not engine.startswith("mms"):
        return text
    if lang == "en":
        return re.sub(r"\d+", lambda m: english_number(int(m.group(0))), text)
    words = DIGITS.get(lang)
    if not words:
        return text
    w = words.split()
    return re.sub(r"\d+", lambda m: " ".join(w[int(d)] for d in m.group(0)), text)


def keep_within(parts: list[dict[str, Any]], limit: float) -> list[dict[str, Any]]:
    """Which sentences fit in `limit` seconds, in their order. The first sentence (what is wrong) and the
    disclaimer (prices are typical, confirm with the dealer) are kept; the reply options go first (the text
    message carries them), then other sentences from the end."""
    keep = list(parts)
    total = lambda xs: sum(p["seconds"] for p in xs)  # noqa: E731
    for p in [p for p in keep if p["reply"]]:
        if total(keep) <= limit:
            break
        keep.remove(p)
    for p in reversed(list(keep)):
        if total(keep) <= limit:
            break
        if p is not keep[0] and not p["disclaimer"]:
            keep.remove(p)
    while total(keep) > limit and len(keep) > 1:  # still too long: the disclaimer alone stays with the first
        keep.remove(next(p for p in reversed(keep) if p is not keep[0] and not p["disclaimer"]) if any(
            p is not keep[0] and not p["disclaimer"] for p in keep) else keep[-1])  # fmt: skip
    return keep


# ---------------------------------------------------------------------- engines
def _piper(name: str) -> Any:
    import sherpa_onnx

    d = MODELS / name
    onnx = next(d.glob("*.onnx"))
    tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
        vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(onnx), tokens=str(d / "tokens.txt"),
                                                   data_dir=str(d / "espeak-ng-data")),
        num_threads=max(1, (os.cpu_count() or 4) // 2))))  # fmt: skip

    def say(text: str) -> tuple[np.ndarray, int]:
        a = tts.generate(text, sid=0, speed=1.0)
        return np.asarray(a.samples, dtype=np.float32), int(a.sample_rate)

    return say


def local_or(repo: str) -> str:
    """A model fetched by scripts/fetch_voice_models (data/tts/models/<name>), else the hub id."""
    d = MODELS / repo.split("/")[-1]
    return str(d) if d.exists() else repo


def _mms(repo: str) -> Any:
    import torch
    from transformers import AutoTokenizer, VitsModel

    path = local_or(repo)
    model, tok = VitsModel.from_pretrained(path), AutoTokenizer.from_pretrained(path)

    def say(text: str) -> tuple[np.ndarray, int]:
        with torch.no_grad():
            out = model(**tok(text, return_tensors="pt")).waveform
        return out[0].numpy().astype(np.float32), int(model.config.sampling_rate)

    return say


PIPER = {"piper-hi-pratham": "vits-piper-hi_IN-pratham-medium", "piper-hi-priyamvada": "vits-piper-hi_IN-priyamvada-medium",
         "piper-hi-rohan": "vits-piper-hi_IN-rohan-medium"}  # fmt: skip
MMS = {"mms-hi": "facebook/mms-tts-hin", "mms-mr": "facebook/mms-tts-mar", "mms-gu": "facebook/mms-tts-guj",
       "mms-ta": "facebook/mms-tts-tam", "mms-en": "facebook/mms-tts-eng"}  # fmt: skip


def load(engine: str) -> Any:
    with _lock:
        if engine not in _engines:
            _engines[engine] = _piper(PIPER[engine]) if engine in PIPER else _mms(MMS[engine])
        return _engines[engine]


def to_ogg(wav: np.ndarray, sr: int) -> bytes:
    """OGG / Opus mono, what WhatsApp plays as a voice note (PyAV)."""
    import av

    rate = 48000
    if sr != rate:
        import librosa

        wav = librosa.resample(wav.astype(np.float32), orig_sr=sr, target_sr=rate)
    pcm = (np.clip(wav, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with av.open(buf, "w", format="ogg") as out:
        st = out.add_stream("libopus", rate=rate, layout="mono")
        st.bit_rate = 24000
        for i in range(0, len(pcm), 960 * 50):
            frame = av.AudioFrame.from_ndarray(pcm[None, i : i + 960 * 50], format="s16", layout="mono")
            frame.sample_rate = rate
            for p in st.encode(frame):
                out.mux(p)
        for p in st.encode(None):
            out.mux(p)
    return buf.getvalue()


def note(
    template: str, values: dict[str, Any], lang: str, text: str, engine: str | None = None
) -> dict[str, Any] | None:
    """The voice note for this template text, from the cache or synthesised now. None when switched off,
    when no engine covers the language, or on any failure (the text alert goes out regardless)."""
    if not enabled() or not text.strip():
        return None
    vlang = VOICE_LANG.get(lang, lang)
    engine = engine or engine_for(vlang)
    if engine is None:
        return None
    path = cache_path(template, values, vlang, engine)
    meta = path.with_suffix(".json")
    if path.exists() and meta.exists():
        return {**json.loads(meta.read_text(encoding="utf-8")), "path": str(path), "cached": True}
    try:
        say = load(engine)
        parts, sr = [], 16000
        for s in sentences(text):  # the template's own sentences, marked before they are spelled out
            words = speakable(spoken(s, vlang), vlang, engine)
            if not words.strip(" .,"):
                continue
            wav, sr = say(words)
            parts.append({"text": words, "wav": wav, "seconds": len(wav) / sr + GAP_S,
                          "reply": bool(re.search(r"\b1\s*=", s)), "disclaimer": s.strip().startswith("(")})  # fmt: skip
        kept = keep_within(parts, MAX_SECONDS)  # the text message carries anything left out
        if not kept:
            return None
        gap = np.zeros(int(GAP_S * sr), np.float32)
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(to_ogg(np.concatenate([x for p in kept for x in (p["wav"], gap)]), sr))
        info = {"seconds": round(sum(p["seconds"] for p in kept), 1), "engine": engine, "lang": vlang,
                "sentences": len(kept), "of": len(parts), "template": template,
                "spoken": " ".join(p["text"] for p in kept)}  # fmt: skip
        meta.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        return {**info, "path": str(path), "cached": False}
    except Exception as e:  # a voice that cannot be made must never block the alert
        return {"error": f"{type(e).__name__}: {e}"[:200], "engine": engine, "path": None}
