"""What an alert carries besides text: a 5 s before/now clip (OGG Opus, the audio WhatsApp plays inline)
and a before | now spectrogram image."""

import io

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw

from edge import features
from edge.config import SR

CLIP_S = 5.0
GAP_S = 0.25


def before_now_clip(before: np.ndarray, now: np.ndarray, start_s: float = 0.0) -> bytes:
    """About 2.4 s of the machine's normal sound, a short gap, then the same stretch now."""
    half = int((CLIP_S - GAP_S) / 2 * SR)
    a = int(start_s * SR)

    def seg(x: np.ndarray) -> np.ndarray:
        s = x[a : a + half]
        if len(s) < half:  # short recordings (e.g. 1 s vibration snapshots) are looped
            s = np.resize(x, half)
        peak = float(np.max(np.abs(s))) or 1.0
        return (s / peak * 0.8).astype(np.float32)  # same loudness for both, so the change is in the sound

    out = np.concatenate([seg(before), np.zeros(int(GAP_S * SR), np.float32), seg(now)])
    buf = io.BytesIO()
    sf.write(buf, out, SR, format="OGG", subtype="OPUS")
    return buf.getvalue()


def _panel(audio: np.ndarray, w: int, h: int, hot: tuple[int, int] | None) -> Image.Image:
    mel = features.log_mel(audio[: int(4 * SR)])
    lo, hi = np.percentile(mel, [5, 99.5])
    v = np.clip((mel - lo) / (hi - lo + 1e-9), 0, 1)[::-1]  # low pitch at the bottom
    rgb = np.stack([28 + v * 200, 30 + v * 198, 34 + v * 196], axis=-1)
    if hot:  # the band that changed, tinted orange
        rows = slice(v.shape[0] - 1 - hot[1], v.shape[0] - hot[0])
        rgb[rows] = np.stack([40 + v[rows] * 215, 30 + v[rows] * 76, 25 + v[rows] * 0], axis=-1)
    return Image.fromarray(rgb.astype(np.uint8)).resize((w, h), Image.NEAREST)


def spectrogram_png(before: np.ndarray, now: np.ndarray, hot: tuple[int, int] | None = None) -> bytes:
    w, h, pad, top = 360, 220, 12, 28
    img = Image.new("RGB", (2 * w + 3 * pad, h + top + pad), (18, 20, 23))
    d = ImageDraw.Draw(img)
    for i, (label, audio) in enumerate((("before (its normal)", before), ("now", now))):
        x = pad + i * (w + pad)
        img.paste(_panel(audio, w, h, hot), (x, top))
        d.text((x, 8), label, fill=(233, 231, 226))
    d.text((2 * w + 2 * pad - 150, 8), "time -> , pitch ^", fill=(142, 145, 152))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
