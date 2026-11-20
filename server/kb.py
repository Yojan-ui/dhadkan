"""The knowledge base: what Granite retrieves before it plans (RAG), and the only other place ₹ may come from.

    hits = kb.search("blower khat-khat, bearing housing hot", fault="bearing_wear", machine_type="fan")
    -> [{"id": "kb/bearing_wear.md#hand-check-machine-off", "title": ..., "text": ..., "score": 0.71,
         "method": "hybrid", "source": "kb/bearing_wear.md", "faults": [...], "machines": [...]}, ...]

    python -m server.kb build           embed every chunk with granite-embedding (cached in data/kb/)
    python -m server.kb search "pump makes gravel noise" [--fault cavitation] [--machine pump]

Chunks:
  * kb/*.md (not README): one chunk per `## ` section, with the guide's front matter (faults, machines).
    Guides hold no ₹ amount and no part number (tests/test_kb.py).
  * server/faults.yaml: one chunk per fault card, rendered by code: part, price range with its unit and
    source, labour, technician, repairable, misuse, warranty, what happens if ignored. Every figure is the
    file's own, marked TYPICAL, NOT A QUOTE. So a ₹ amount the planner quotes from the KB is a faults.yaml
    figure, and the grounding check accepts it only because it is in a retrieved chunk.

Search is hybrid: cosine similarity of granite-embedding vectors (when Ollama answers and the index is
built) blended with BM25 keyword scores, plus a boost for chunks tagged with the fault or machine type
asked about. Offline it is BM25 alone (method "keyword"): the planner still gets the right card.
The index is keyed by a hash of each chunk's text and the embedding model, so an edited guide is
re-embedded and nothing stale is served.
"""

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from . import provider
from .config import FAULTS, ROOT

KB_DIR = ROOT / "kb"
INDEX = ROOT / "data" / "kb" / "index.npz"
W_VEC, W_KEY, W_TAG = 0.65, 0.35, 1.0  # blend of the vector and keyword scores, and the tag boost
_TOKEN = re.compile(r"[\w]+", re.UNICODE)
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "with",
        "when",
        "which",
        "while",
        "will",
        "not",
        "no",
        "can",
        "do",
        "does",
    ]
)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _stem(t: str) -> str:
    """Plural to singular, enough for keyword search (looms -> loom, bearings -> bearing, fixes -> fix)."""
    if len(t) > 4 and t.endswith(("ches", "shes", "xes", "sses")):
        return t[:-2]
    if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        return t[:-1]
    return t


def tokens(text: str) -> list[str]:
    return [
        _stem(t) for t in (m.group(0).lower() for m in _TOKEN.finditer(text)) if t not in _STOP and len(t) > 1
    ]


# ---------------------------------------------------------------------- chunks
def _front(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    _, head, body = text.split("---", 2)
    return yaml.safe_load(head) or {}, body


def guide_chunks(kb_dir: Path = KB_DIR) -> list[dict[str, Any]]:
    out = []
    for p in sorted(kb_dir.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        meta, body = _front(p.read_text(encoding="utf-8"))
        rel = f"kb/{p.name}"
        for sec in re.split(r"(?m)^## ", body)[1:]:
            head, _, text = sec.partition("\n")
            text = " ".join(text.split())
            if text:
                out.append({"id": f"{rel}#{slug(head)}", "title": f"{meta.get('title', p.stem)}: {head.strip()}",
                            "text": text, "source": rel, "kind": "guide", "faults": list(meta.get("faults") or []),
                            "machines": list(meta.get("machines") or ["any"])})  # fmt: skip
    return out


def _rng(v: Any, unit: str = "") -> str:
    if not v:
        return "no published figure"
    lo, hi = v
    return f"{lo:,} to {hi:,}{unit}"


def price_text(fix: dict[str, Any]) -> str:
    """The card's price exactly as faults.yaml has it: a range, a per-HP rate, or no published figure."""
    if fix.get("price_inr"):
        lo, hi = fix["price_inr"]
        return f"₹{lo:,} to ₹{hi:,}"
    if fix.get("price_inr_per_hp"):
        return f"₹{fix['price_inr_per_hp']:,} per HP"
    return "not published"


def fault_chunks(path: Path = FAULTS) -> list[dict[str, Any]]:
    """One chunk per faults.yaml card, in English, every figure as the file has it."""
    kb = yaml.safe_load(path.read_text(encoding="utf-8"))
    src = kb.get("sources", {})
    machines = {
        "cavitation": ["pump"],
        "imbalance": ["fan", "pump"],
        "belt_slip": ["fan", "slider", "gearbox"],
    }
    out = []
    for key, f in kb["faults"].items():
        fix, rep, war = f.get("fix", {}), f.get("repairable", {}), f.get("warranty", {}) or {}
        s = src.get(fix.get("price_source") or "", {})
        lines = [
            f"Fault {key}: {f['name']['en']}. {f['what']['en']}",
            f"Repairable: {'yes' if rep.get('answer') else 'no'}. {rep.get('why', '')}"
            + (f" Not repairable when {rep['not_when']}." if rep.get("not_when") else ""),
            f"Fix: {fix.get('part', {}).get('en', '')}. Typical price {price_text(fix)}"
            f" ({fix.get('price_unit', '')}); source: {s.get('title', 'none, an assumption')}"
            + (f", accessed {s['accessed']}" if s.get("accessed") else "") + ". TYPICAL, NOT A QUOTE.",
            f"Labour: {_rng(fix.get('labour_hours'), ' hours')}; technician: {fix.get('technician', {}).get('en', '')}.",
            "Common causes: " + "; ".join(m["en"] for m in f.get("misuse", []) or []) + ".",
            f"Warranty: {war.get('period') or 'no published period'}. {war.get('note', '')}"
            + (" Voided by: " + "; ".join(v["en"] for v in war.get("voided_by", [])) + "." if war.get("voided_by") else ""),
            f"If ignored: {f.get('if_ignored', {}).get('what', {}).get('en', '')}.",
        ]  # fmt: skip
        out.append({"id": f"faults.yaml#{key}", "title": f"Fault card: {f['name']['en']}", "text": " ".join(lines),
                    "source": "server/faults.yaml", "kind": "fault_card", "faults": [key],
                    "machines": machines.get(key, ["any"]), "url": s.get("url")})  # fmt: skip
    return out


@lru_cache(maxsize=4)
def _chunks_cached(kb_dir: str, faults: str, stamp: str) -> tuple[dict[str, Any], ...]:
    return tuple(guide_chunks(Path(kb_dir)) + fault_chunks(Path(faults)))


def chunks(kb_dir: Path = KB_DIR, faults: Path = FAULTS) -> list[dict[str, Any]]:
    stamp = ";".join(f"{p.name}:{p.stat().st_mtime_ns}" for p in [*sorted(kb_dir.glob("*.md")), faults])
    return [dict(c) for c in _chunks_cached(str(kb_dir), str(faults), stamp)]


def chunk_hash(c: dict[str, Any], model: str) -> str:
    return hashlib.sha256(f"{model}\n{c['title']}\n{c['text']}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------- the vector index
def build(index: Path = INDEX, cs: list[dict[str, Any]] | None = None, batch: int = 16) -> dict[str, Any]:
    """Embed every chunk not already in the index (by text hash). Raises OSError when Ollama is down."""
    cs = cs if cs is not None else chunks()
    model = provider.embed_model()
    old = load_index(index)
    have = dict(zip(old["hashes"], old["vectors"], strict=True)) if old else {}
    hashes = [chunk_hash(c, model) for c in cs]
    todo = [i for i, h in enumerate(hashes) if h not in have]
    for k in range(0, len(todo), batch):
        part = todo[k : k + batch]
        vecs = provider.embed([f"{cs[i]['title']}\n{cs[i]['text']}" for i in part], model)
        for i, v in zip(part, vecs, strict=True):
            have[hashes[i]] = np.asarray(v, dtype=np.float32)
    mat = np.stack([have[h] for h in hashes]) if hashes else np.zeros((0, 1), np.float32)
    mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    index.parent.mkdir(parents=True, exist_ok=True)
    np.savez(index, vectors=mat, hashes=np.array(hashes), model=np.array(model))
    load_index.cache_clear()
    return {"chunks": len(cs), "embedded": len(todo), "model": model, "path": str(index)}


@lru_cache(maxsize=4)
def load_index(index: Path = INDEX) -> dict[str, Any] | None:
    if not index.exists():
        return None
    z = np.load(index, allow_pickle=False)
    return {"vectors": z["vectors"], "hashes": [str(h) for h in z["hashes"]], "model": str(z["model"])}


# ---------------------------------------------------------------------- search
def bm25(query: str, cs: list[dict[str, Any]], k1: float = 1.5, b: float = 0.75) -> np.ndarray:
    docs = [tokens(c["title"] + " " + c["text"] + " " + " ".join(c["faults"])) for c in cs]
    q = tokens(query)
    if not docs or not q:
        return np.zeros(len(docs))
    avg = sum(map(len, docs)) / len(docs)
    df = Counter(t for d in docs for t in set(d))
    out = np.zeros(len(docs))
    for i, d in enumerate(docs):
        tf = Counter(d)
        for t in q:
            if tf[t]:
                idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                out[i] += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avg))
    return out


def _vector_scores(query: str, cs: list[dict[str, Any]], index: Path) -> np.ndarray | None:
    """Cosine similarity per chunk, or None when the index is missing, stale, or Ollama is down."""
    idx = load_index(index)
    model = provider.embed_model()
    if idx is None or idx["model"] != model or not provider.available(model):
        return None
    pos = {h: i for i, h in enumerate(idx["hashes"])}
    rows = [pos.get(chunk_hash(c, model)) for c in cs]
    if any(r is None for r in rows):  # a guide changed since the last build: do not serve stale vectors
        return None
    try:
        q = np.asarray(provider.embed([query], model)[0], dtype=np.float32)
    except (OSError, ValueError, KeyError):
        return None
    q = q / (np.linalg.norm(q) + 1e-9)
    return idx["vectors"][rows] @ q


def search(query: str, k: int = 4, fault: str | None = None, machine_type: str | None = None,
           index: Path = INDEX, cs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:  # fmt: skip
    cs = cs if cs is not None else chunks()
    if not cs:
        return []
    key = bm25(" ".join(x for x in (query, fault or "", machine_type or "") if x), cs)
    key = key / key.max() if key.max() > 0 else key
    vec = _vector_scores(query, cs, index)
    # a named fault is a filter in all but name: its chunks outrank any keyword overlap; the machine type nudges
    tag = np.array([(1.0 if fault and fault in c["faults"] else 0)
                    + (0.2 if machine_type and (machine_type in c["machines"] or "any" in c["machines"]) else 0)
                    for c in cs])  # fmt: skip
    score = (W_VEC * np.clip(vec, 0, 1) + W_KEY * key if vec is not None else key) + W_TAG * tag
    method = "hybrid" if vec is not None else "keyword"
    order = np.argsort(-score, kind="stable")[:k]
    return [{**cs[i], "score": round(float(score[i]), 3), "method": method} for i in order if score[i] > 0]


def for_case(machine_type: str | None, fault: str | None, text: str = "", k: int = 3) -> list[dict[str, Any]]:
    """The chunks the planner gets up front for an alarm: the fault card first when the fault is known."""
    q = " ".join(
        x for x in (text, (fault or "").replace("_", " "), machine_type or "", "symptoms hand check") if x
    )
    hits = search(q, k=k + 1, fault=fault, machine_type=machine_type)
    if fault:  # the named fault's card always leads: it carries the figures the planner may quote
        card = next((c for c in chunks() if c["id"] == f"faults.yaml#{fault}"), None)
        if card:
            hits = [
                {**card, "score": None, "method": "fault card"},
                *(h for h in hits if h["id"] != card["id"]),
            ]
    return hits[:k]


def brief(hits: list[dict[str, Any]], width: int = 900) -> list[dict[str, Any]]:
    """What goes to the model: id (to cite), title, source, text (trimmed)."""
    return [
        {"id": h["id"], "title": h["title"], "source": h["source"], "text": h["text"][:width]} for h in hits
    ]


def ensure_index() -> dict[str, Any] | None:
    """Build or refresh the vector index when Ollama has the embedder; None (keyword search) otherwise.
    Never raises: the server calls it on a background thread at start-up."""
    try:
        return build() if provider.available(provider.embed_model()) else None
    except Exception:  # an index we cannot build is not an error: search uses keywords
        return None


EVAL = KB_DIR / "eval_questions.json"


def evaluate(index: Path = INDEX, questions: Path = EVAL) -> dict[str, Any]:
    """Retrieval quality on labelled questions: a hit is a chunk tagged with the expected fault.
    Keyword mode always; hybrid mode when Ollama and the index are there. No fault hint is given."""
    qs = json.loads(questions.read_text(encoding="utf-8"))["questions"]
    cs = chunks()
    modes: dict[str, Any] = {}
    for mode in ("keyword", "hybrid"):
        rows = []
        for q in qs:
            if mode == "keyword":
                saved = provider.available
                provider.available = lambda model=None: False  # type: ignore[assignment]
                try:
                    hits = search(q["q"], k=5, machine_type=q.get("machine"), index=index, cs=cs)
                finally:
                    provider.available = saved  # type: ignore[assignment]
            else:
                hits = search(q["q"], k=5, machine_type=q.get("machine"), index=index, cs=cs)
                if hits and hits[0]["method"] != "hybrid":
                    break
            rank = next((i + 1 for i, h in enumerate(hits) if q["fault"] in h["faults"]), None)
            rows.append(
                {"q": q["q"], "want": q["fault"], "rank": rank, "top": hits[0]["id"] if hits else None}
            )
        if len(rows) < len(qs):
            modes[mode] = {"skipped": "Ollama or the vector index is not available"}
            continue
        n = len(rows)
        modes[mode] = {"hit@1": round(sum(r["rank"] == 1 for r in rows) / n, 3),
                       "hit@3": round(sum(bool(r["rank"]) and r["rank"] <= 3 for r in rows) / n, 3),
                       "mrr": round(sum(1 / r["rank"] for r in rows if r["rank"]) / n, 3),
                       "misses_at_1": [r for r in rows if r["rank"] != 1]}  # fmt: skip
    return {"questions": len(qs), "chunks": len(cs), "embed_model": provider.embed_model(), "modes": modes}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="python -m server.kb")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="embed the chunks with granite-embedding (Ollama)")
    s = sub.add_parser("search", help="search the knowledge base")
    s.add_argument("query")
    s.add_argument("--fault")
    s.add_argument("--machine")
    s.add_argument("-k", type=int, default=4)
    sub.add_parser("eval", help="retrieval quality on kb/eval_questions.json -> docs/runs/kb_eval_<UTC>.json")
    a = ap.parse_args()
    if a.cmd == "eval":
        import time

        from edge.config import RUNS

        rec = {"run_id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), **evaluate()}
        RUNS.mkdir(parents=True, exist_ok=True)
        out = RUNS / f"kb_eval_{rec['run_id']}.json"
        out.write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
        for m, v in rec["modes"].items():
            print(m, {k: v[k] for k in ("hit@1", "hit@3", "mrr")} if "mrr" in v else v)
        print(out)
        return 0
    if a.cmd == "build":
        try:
            print(json.dumps(build(), indent=1))
        except OSError as e:
            print(
                f"Ollama not answering ({type(e).__name__}); start it and run: ollama pull {provider.embed_model()}"
            )
            print("Search still works offline, by keywords.")
            return 1
        return 0
    for h in search(a.query, k=a.k, fault=a.fault, machine_type=a.machine):
        print(f"{h['score']:>6}  {h['method']:<7}  {h['id']}\n        {h['text'][:160]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
