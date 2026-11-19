"""Owner's words about a sound -> weak fault label.

    "पंप से खट-खट आवाज़ आ रही है"  -> sound khat-khat -> bearing (weak)

Order: transcribe (Whisper, local) -> lexicon match -> Granite (local) only if the
lexicon finds nothing. The result is a *weak* label: the owner's description,
not an inspection.
"""

import json
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import provider
from .config import LEXICON

# (text, lexicon) -> {sound, fault, ...}: the Granite fallback, or a stub in tests
LLM = Callable[[str, dict[str, Any]], dict[str, Any]]


def load_lexicon(path: Path = LEXICON) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# Speech-to-text often swaps retroflex and dental consonants and drops or adds
# the halant ("खत्खत" for "खटखट"), so match on a folded spelling.
_FOLD = str.maketrans(
    {"ट": "त", "ठ": "थ", "ड": "द", "ढ": "ध", "ण": "न", "ळ": "ल", "़": None, "्": None, "ँ": "ं"}
)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text.lower()).translate(_FOLD)
    return re.sub(r"[\s\-_,.!?।]+", " ", text).strip()


def extract(text: str, lexicon: dict[str, Any]) -> list[dict[str, Any]]:
    """All lexicon sounds mentioned in `text`, first mention first."""
    t = f" {_norm(text)} "
    hits = []
    for entry in lexicon["sounds"]:
        for form in entry["forms"]:
            pos = t.find(f" {_norm(form)}")  # must start a word
            if pos >= 0:
                hits.append((pos, entry, form))
                break
    return [
        {"sound": e["sound"], "matched": form, "faults": e["faults"]}
        for _, e, form in sorted(hits, key=lambda h: h[0])
    ]


def combine(hits: list[dict[str, Any]], faults: list[str]) -> dict[str, float]:
    scores = dict.fromkeys(faults, 0.0)
    for h in hits:
        for f, w in h["faults"].items():
            scores[f] += w / len(hits)
    return scores


def label_text(text: str, lexicon: dict[str, Any] | None = None, llm: LLM | None = None) -> dict[str, Any]:
    """-> {sound, fault, scores, source}. `llm` is a callable used only on a miss."""
    lexicon = lexicon or load_lexicon()
    hits = extract(text, lexicon)
    if hits:
        scores = combine(hits, lexicon["faults"])
        return {
            "sound": "+".join(h["sound"] for h in hits),
            "fault": max(scores, key=scores.get),
            "scores": scores,
            "source": "lexicon",
        }
    if llm is None:
        return {"sound": None, "fault": "unknown", "scores": {}, "source": "none"}
    return llm(text, lexicon)


def llm_label(text: str, lexicon: dict[str, Any], client: Any = None) -> dict[str, Any]:
    """Fallback for sounds the lexicon doesn't know: Granite on the local Ollama."""
    client = client or provider.make_client()
    if client is None:
        return {"sound": None, "fault": "unknown", "scores": {}, "source": "none"}
    faults = lexicon["faults"] + ["unknown"]
    schema = {
        "type": "object",
        "properties": {
            "sound": {"type": "string", "description": "the sound word(s) as the owner wrote them, or empty"},
            "fault": {"type": "string", "enum": faults},
            "why": {"type": "string"},
        },
        "required": ["sound", "fault", "why"],
        "additionalProperties": False,
    }
    known = "; ".join(f"{s['sound']} = {s['meaning']}" for s in lexicon["sounds"])
    resp = client.messages.create(
        model=provider.chat_model(),
        max_tokens=1024,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        system=(
            "A small-factory owner in India describes a machine sound in Hindi, Marathi or "
            "English. Pull out the sound word they used and map it to one fault class: "
            f"{', '.join(lexicon['faults'])}, or unknown if the text gives no basis. "
            f"Known sound words for reference: {known}."
        ),
        messages=[{"role": "user", "content": text}],
    )
    if resp.stop_reason == "refusal":
        return {"sound": None, "fault": "unknown", "scores": {}, "source": "llm_refused"}
    try:
        out = json.loads(next(b.text for b in resp.content if b.type == "text"))
        assert out["fault"] in faults  # code validates what the model proposed
    except (StopIteration, ValueError, KeyError, AssertionError):
        return {"sound": None, "fault": "unknown", "scores": {}, "source": "llm_invalid"}
    return {
        "sound": out["sound"] or None,
        "fault": out["fault"],
        "scores": {out["fault"]: 1.0} if out["fault"] != "unknown" else {},
        "source": "llm",
        "why": out["why"],
    }


_whisper = {}


def transcribe(path: str | Path, language: str | None = None, model_size: str = "small") -> tuple[str, str]:
    """Local speech-to-text (faster-whisper). Audio is not stored or sent anywhere.

    `language` (hi/mr/en) comes from the owner's profile; Whisper guesses Marathi
    as Hindi otherwise.
    """
    import librosa
    from faster_whisper import WhisperModel

    if model_size not in _whisper:
        _whisper[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    audio, _ = librosa.load(str(path), sr=16_000, mono=True)  # wav/ogg/mp3 -> 16 kHz array
    segments, info = _whisper[model_size].transcribe(audio, beam_size=5, language=language)
    return " ".join(s.text.strip() for s in segments), info.language


def asr_check(repeats: int = 3) -> Path:
    """Transcribe each sample voice note `repeats` times and log every attempt.

    int8 CPU decoding is not bit-stable, so the same file can come out differently.
    Writes runs/asr_<ts>.json.
    """
    import time

    from edge.config import ROOT, RUNS

    notes = sorted((ROOT / "data" / "sim" / "voice_notes").glob("*.wav"))
    out = {
        "run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "model": "faster-whisper small int8",
        "note": "synthetic TTS voice notes (macOS 'Lekha' hi_IN), not real owners",
        "results": [],
    }
    lex = load_lexicon()
    for f in notes:
        lang = "mr" if "_marathi_" in f.name else "hi"
        for attempt in range(repeats):
            _whisper.clear()  # fresh model each attempt
            text, _ = transcribe(f, language=lang)
            out["results"].append(
                {
                    "file": f.name,
                    "attempt": attempt + 1,
                    "language_hint": lang,
                    "transcript": text,
                    "label": label_text(text, lex),
                }
            )
    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"asr_{out['run_id']}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


if __name__ == "__main__":
    print(asr_check())
