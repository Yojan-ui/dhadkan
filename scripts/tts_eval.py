"""Which text-to-speech should read Dhadkan's alerts aloud? Quality and latency on THIS laptop, recorded.

    python -m scripts.tts_eval [--whisper small]          (.\\dev.ps1 tts-eval)  -> docs/runs/tts_<UTC>.json

The texts are the real templates: an owner alert rendered by server/diagnosis.message (hi, mr, gu, en) and
the daily digest head (server/notify/policy). Voice is always made from template text, never from LLM text.

Candidates, in the order asked for:
  1. AI4Bharat (IndicF5, Indic Parler-TTS): gated on Hugging Face (accept the terms with an account, then
     set HF_TOKEN). Without a token they are recorded as not evaluated, with the reason.
  2. CPU / edge-friendly:
       * Piper voices (VITS, ONNX) through sherpa-onnx: hi_IN pratham, priyamvada, rohan (Hindi only);
       * Meta MMS-TTS (VITS) through transformers on CPU: hin, mar, guj, tam (+ eng). CC-BY-NC-4.0;
       * Windows' own voices (SAPI): only those installed on this machine.
  3. Pre-recorded: every voice note is cached per (template, values, language), so a repeated alert costs
     a file read; measured as the cache's own latency.

Quality has no listener here, so it is measured by round trip: the audio is transcribed again with
faster-whisper in that language and compared with the source text (character error rate, CER, after
removing punctuation; lower is better). Latency is the real-time factor (synthesis seconds / audio
seconds) on this CPU. Both are recorded per engine and language, with the license.
"""

import argparse
import json
import os
import re
import sys
import tarfile
import time
import unicodedata
import urllib.request
from typing import Any

import numpy as np
import soundfile as sf

from edge.config import ROOT, RUNS

MODELS = ROOT / "data" / "tts" / "models"
SAMPLES = ROOT / "data" / "tts" / "samples"
PIPER = {
    "piper-hi-pratham": "vits-piper-hi_IN-pratham-medium",
    "piper-hi-priyamvada": "vits-piper-hi_IN-priyamvada-medium",
    "piper-hi-rohan": "vits-piper-hi_IN-rohan-medium",
}
PIPER_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{name}.tar.bz2"
MMS = {"hi": "facebook/mms-tts-hin", "mr": "facebook/mms-tts-mar", "gu": "facebook/mms-tts-guj",
       "ta": "facebook/mms-tts-tam", "en": "facebook/mms-tts-eng"}  # fmt: skip
WHISPER_LANG = {"hi": "hi", "mr": "mr", "gu": "gu", "ta": "ta", "en": "en"}


def texts() -> dict[str, list[str]]:
    """Template sentences per language: the alert body and the digest head, as the owner would get them."""
    from server import diagnosis, voice
    from server.notify import policy

    machine = {"id": "A-G1", "type": "gearbox", "status": "alarm", "known_fault": "bearing",
               "forecast": {"status": "estimate", "low": 6, "high": 12, "crossings": [4, 6, 8, 9, 12, 30]}}  # fmt: skip
    names = {"hi": "लूम 1", "mr": "लूम 1", "gu": "લૂમ 1", "en": "Loom 1", "ta": "Loom 1"}
    out = {}
    for lang in ("hi", "mr", "gu", "en"):
        c = diagnosis.card(machine, "A", "bearing_wear", hp=2, service_h=1.5, lang=lang)
        # the spoken form of the template text (server/voice.spoken): what an engine actually reads
        out[lang] = [
            voice.spoken(diagnosis.message(c, names[lang], 3, lang), lang),
            voice.spoken(policy.DIGEST_HEAD[lang], lang),
        ]
    return out


def norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s.lower())
    s = re.sub(r"[^\w\s]|_", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def cer(ref: str, hyp: str) -> float:
    a, b = norm(ref), norm(hyp)
    if not a:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return round(prev[-1] / len(a), 3)


# ---------------------------------------------------------------------- engines
def piper_engine(key: str) -> Any:
    import sherpa_onnx

    name = PIPER[key]
    d = MODELS / name
    if not d.exists():
        MODELS.mkdir(parents=True, exist_ok=True)
        arc = MODELS / f"{name}.tar.bz2"
        if not arc.exists():
            print(f"  downloading {name} ...")
            urllib.request.urlretrieve(PIPER_URL.format(name=name), arc)
        with tarfile.open(arc, "r:bz2") as t:
            t.extractall(MODELS, filter="data")
    onnx = next(p for p in d.glob("*.onnx"))
    cfg = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
        vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(onnx), tokens=str(d / "tokens.txt"),
                                                   data_dir=str(d / "espeak-ng-data")),
        num_threads=max(1, (os.cpu_count() or 4) // 2)))  # fmt: skip
    tts = sherpa_onnx.OfflineTts(cfg)

    def say(text: str) -> tuple[np.ndarray, int]:
        a = tts.generate(text, sid=0, speed=1.0)
        return np.asarray(a.samples, dtype=np.float32), int(a.sample_rate)

    return say, {"languages": ["hi"], "license": "see the voice's MODEL_CARD (Piper voices vary)", "runtime": "onnx (sherpa-onnx)",
                 "size_mb": round(sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) / 1e6, 1)}  # fmt: skip


def mms_engine(lang: str) -> Any:
    import torch
    from transformers import AutoTokenizer, VitsModel

    torch.set_num_threads(max(1, (os.cpu_count() or 4) // 2))
    from server.voice import local_or

    repo = local_or(MMS[lang])
    model, tok = VitsModel.from_pretrained(repo), AutoTokenizer.from_pretrained(repo)

    def say(text: str) -> tuple[np.ndarray, int]:
        with torch.no_grad():
            out = model(**tok(text, return_tensors="pt")).waveform
        return out[0].numpy().astype(np.float32), int(model.config.sampling_rate)

    size = sum(p.numel() for p in model.parameters()) * 4 / 1e6
    return say, {"languages": [lang], "license": "CC-BY-NC-4.0 (non-commercial)", "runtime": "torch CPU (transformers VITS)",
                 "size_mb": round(size, 1)}  # fmt: skip


def sapi_voices() -> list[str]:
    if sys.platform != "win32":
        return []
    import subprocess

    ps = ("Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
          ".GetInstalledVoices() | % { $_.VoiceInfo.Culture.Name + '|' + $_.VoiceInfo.Name }")  # fmt: skip
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30
        )
        return [x.strip() for x in out.stdout.splitlines() if x.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


# ---------------------------------------------------------------------- the run
def run(whisper: str = "small") -> dict[str, Any]:
    from faster_whisper import WhisperModel

    local = MODELS / f"faster-whisper-{whisper}"
    asr = WhisperModel(str(local) if local.exists() else whisper, device="cpu", compute_type="int8")
    T = texts()
    SAMPLES.mkdir(parents=True, exist_ok=True)
    rows, engines = [], {}

    def measure(engine: str, say: Any, lang: str, meta: dict[str, Any]) -> None:
        for k, text in enumerate(T[lang]):
            say(text[:40])  # warm-up, not timed
            t0 = time.perf_counter()
            wav, sr = say(text)
            secs = time.perf_counter() - t0
            dur = len(wav) / sr
            path = SAMPLES / f"{engine}_{lang}_{k}.wav"
            sf.write(path, wav, sr)
            import librosa  # decode here: faster-whisper's own file decoder needs an older PyAV than this one

            pcm16 = (
                librosa.resample(wav.astype(np.float32), orig_sr=sr, target_sr=16000) if sr != 16000 else wav
            )
            segs, _ = asr.transcribe(pcm16.astype(np.float32), language=WHISPER_LANG[lang], beam_size=5)
            heard = " ".join(s.text for s in segs).strip()
            rows.append({"engine": engine, "lang": lang, "text": k, "chars": len(text), "audio_s": round(dur, 2),
                         "synth_s": round(secs, 2), "rtf": round(secs / max(dur, 1e-6), 3), "cer": cer(text, heard),
                         "heard": heard[:300], "sample": str(path.relative_to(ROOT))})  # fmt: skip
            print(f"  {engine:22s} {lang} #{k}: {dur:5.1f}s audio, {secs:5.2f}s synth, CER {rows[-1]['cer']}")
        engines[engine] = meta | {"status": "evaluated"}

    for key in PIPER:
        try:
            say, meta = piper_engine(key)
            measure(key, say, "hi", meta)
        except Exception as e:  # a download or a model that does not load is recorded, not hidden
            engines[key] = {"status": f"failed: {type(e).__name__}: {e}"[:300]}
    for lang in MMS:
        if lang not in T:
            continue
        key = f"mms-{lang}"
        try:
            say, meta = mms_engine(lang)
            measure(key, say, lang, meta)
        except Exception as e:
            engines[key] = {"status": f"failed: {type(e).__name__}: {e}"[:300]}
    for key, repo in (
        ("ai4bharat-indicf5", "ai4bharat/IndicF5"),
        ("ai4bharat-indic-parler", "ai4bharat/indic-parler-tts"),
    ):
        engines[key] = {"status": "not evaluated: gated on Hugging Face (accept the terms, set HF_TOKEN, run again)"
                        if not os.environ.get("HF_TOKEN") else "not evaluated: HF_TOKEN set, but this script has no "
                        "runner for it yet (needs its own package)", "repo": repo}  # fmt: skip
    voices = sapi_voices()
    indian = [v for v in voices if v.split("|")[0] in ("hi-IN", "mr-IN", "gu-IN", "ta-IN")]
    engines["windows-sapi"] = {"status": "evaluated: no voice for hi/mr/gu/ta installed" if not indian else "voices found",
                               "voices": voices}  # fmt: skip
    # pre-recorded: the cache's own latency (a repeated alert is a file read)
    from server import voice

    t0 = time.perf_counter()
    for _ in range(50):
        voice.cache_path("alert_repair", {"name": "Loom 1"}, "hi", "x").exists()
    engines["prerecorded-cache"] = {"status": "evaluated", "lookup_ms": round((time.perf_counter() - t0) / 50 * 1000, 3),
                                    "note": "every voice note is cached per (template, values, language)"}  # fmt: skip
    summary = {}
    for e in {r["engine"] for r in rows}:
        rs = [r for r in rows if r["engine"] == e]
        summary[e] = {"langs": sorted({r["lang"] for r in rs}), "cer_median": round(float(np.median([r["cer"] for r in rs])), 3),
                      "rtf_median": round(float(np.median([r["rtf"] for r in rs])), 3)}  # fmt: skip
    import platform

    return {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), "machine": {"cpu": platform.processor(),
            "threads": os.cpu_count(), "os": platform.platform()}, "asr_for_quality": f"faster-whisper {whisper} int8 CPU",
            "texts": T, "engines": engines, "summary": summary, "rows": rows}  # fmt: skip


def pick(summary: dict[str, Any], engines: dict[str, Any]) -> dict[str, Any]:
    """Per language: the lowest round-trip CER, ties broken by speed (RTF). With the reason and caveats."""
    out = {}
    langs = sorted({lg for s in summary.values() for lg in s["langs"]})
    for lg in langs:
        cand = sorted(((s["cer_median"], s["rtf_median"], e) for e, s in summary.items() if lg in s["langs"]))
        cer_, rtf, e = cand[0]
        note = []
        if cer_ > 0.4:
            note.append(
                "high CER: part of it is faster-whisper's own error on this language; have a native "
                "speaker listen before relying on it"
            )
        if "NC" in str(engines.get(e, {}).get("license", "")):
            note.append("CC-BY-NC-4.0: non-commercial; replace before selling")
        out[lg] = {
            "engine": e,
            "cer_median": cer_,
            "rtf_median": rtf,
            "others": [c[2] for c in cand[1:]],
            "notes": note,
        }
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m scripts.tts_eval")
    ap.add_argument("--whisper", default="small")
    a = ap.parse_args()
    rec = run(a.whisper)
    rec["picked"] = pick(rec["summary"], rec["engines"])
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"tts_{rec['run_id']}.json"
    out.write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
    for e, s in sorted(rec["summary"].items()):
        print(f"{e:22s} {','.join(s['langs']):12s} CER {s['cer_median']:.3f}  RTF {s['rtf_median']:.3f}")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
