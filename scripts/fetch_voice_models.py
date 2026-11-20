"""Download the speech models into data/tts/models (git-ignored), once.

    python -m scripts.fetch_voice_models            (.\\dev.ps1 voice-models)

Uses Python's urllib with normal certificate checks (the operating system's trust store), so it works on
networks where huggingface_hub's own requests session refuses the certificate chain. Nothing is skipped
silently: a file that cannot be fetched is reported and the script exits non-zero.

  faster-whisper-small        speech to text (owners' voice notes), from Systran/faster-whisper-small
  mms-tts-{hin,mar,guj,tam,eng}  Meta MMS-TTS (VITS), CC-BY-NC-4.0
  vits-piper-hi_IN-*-medium   Piper Hindi voices for sherpa-onnx (k2-fsa release)
"""

import json
import sys
import tarfile
import urllib.request
from pathlib import Path

from edge.config import ROOT

MODELS = ROOT / "data" / "tts" / "models"
HF = "https://huggingface.co"
REPOS = {
    "faster-whisper-small": ("Systran/faster-whisper-small", None),
    "mms-tts-hin": ("facebook/mms-tts-hin", None),
    "mms-tts-mar": ("facebook/mms-tts-mar", None),
    "mms-tts-guj": ("facebook/mms-tts-guj", None),
    "mms-tts-tam": ("facebook/mms-tts-tam", None),
    "mms-tts-eng": ("facebook/mms-tts-eng", None),
}
PIPER = [
    "vits-piper-hi_IN-pratham-medium",
    "vits-piper-hi_IN-priyamvada-medium",
    "vits-piper-hi_IN-rohan-medium",
]
PIPER_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{name}.tar.bz2"
SKIP = (".md", ".gitattributes", "README", ".msgpack", ".h5", "flax_model", "tf_model", ".onnx_data")
UA = {"User-Agent": "dhadkan-model-fetch"}


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:  # verified TLS
        return r.read()


def fetch(url: str, out: Path) -> None:
    if out.exists() and out.stat().st_size > 0:
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    with (
        urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r,
        tmp.open("wb") as f,
    ):
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(out)


def hf_repo(local: str, repo: str) -> list[str]:
    files = [s["rfilename"] for s in json.loads(get(f"{HF}/api/models/{repo}"))["siblings"]]
    want = [f for f in files if not any(x in f for x in SKIP)]
    for f in want:
        print(f"  {repo}/{f}")
        fetch(f"{HF}/{repo}/resolve/main/{f}", MODELS / local / f)
    return want


def piper(name: str) -> None:
    if (MODELS / name).exists():
        return
    arc = MODELS / f"{name}.tar.bz2"
    print(f"  {name}")
    fetch(PIPER_URL.format(name=name), arc)
    with tarfile.open(arc, "r:bz2") as t:
        t.extractall(MODELS, filter="data")
    arc.unlink()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failed = []
    for local, (repo, _) in REPOS.items():
        try:
            hf_repo(local, repo)
        except OSError as e:
            failed.append(f"{repo}: {type(e).__name__}: {e}")
    for name in PIPER:
        try:
            piper(name)
        except (OSError, tarfile.TarError) as e:
            failed.append(f"{name}: {type(e).__name__}: {e}")
    for f in failed:
        print("FAILED", f)
    print(f"models in {MODELS}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
