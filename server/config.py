"""Server paths: databases, the simulated cluster, lexicon and the built web app."""

from edge.config import ROOT

DATA = ROOT / "data"
REGISTRY_DB = DATA / "registry.db"
NODE_DB = DATA / "node.db"
CLUSTER = DATA / "sim" / "cluster.json"
PARTS = DATA / "sim" / "parts_mock.json"
VOICE_NOTE = DATA / "sim" / "voice_notes" / "A_hindi_blower_khatkhat.wav"
LEXICON = ROOT / "server" / "lexicon.json"
WEB_DIST = ROOT / "web" / "dist"
