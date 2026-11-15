"""Paths and the few numbers everything else agrees on."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
MODELS = ROOT / "models"
RUNS = ROOT / "runs"
REGISTRY_DB = ROOT / "data" / "registry.db"
NODE_DB = ROOT / "data" / "node.db"
LEXICON = ROOT / "dhadkan" / "lexicon.json"
CLUSTER = ROOT / "sim" / "cluster.json"
PARTS = ROOT / "sim" / "parts_mock.json"

# Front end. The Android app must reproduce exactly these (see models/frontend.json).
SR = 16_000
HOP_WIN_S = 0.5      # windows overlap by half
N_FFT = 1024
HOP = 512
N_MELS = 64
FRAMES = 32          # 32 frames x hop 512 + n_fft = 16 896 samples = 1.06 s per window

EMBED_DIM = N_MELS   # fault fingerprint = per-mel-band residual profile

# Assumptions (shown in the UI, change here):
THRESHOLD_QUANTILE = 0.95   # per-machine alarm = 95th pct of held-out normal clip scores
MATCH_QUANTILE = 0.99       # registry match = above 99th pct of normal windows' similarity
PAUC_MAX_FPR = 0.1          # DCASE convention

MACHINE_TYPES = ("pump", "fan", "gearbox")
