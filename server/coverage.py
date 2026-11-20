"""Kagaz: the machine's own papers (warranty card, insurance policy, invoice, AMC, manual) -> "can I claim this repair?"

The owner uploads each machine's documents once (web app, or the API). Each one is read into fields:
who issued it, number, valid from / till, what it covers and excludes, deductible, how to claim, whom to
call, and the conditions that void it. When Dhadkan raises a fault, `check()` matches the fault against
those fields and says, in the owner's language, whether the repair is likely claimable, from which
paper, the claim steps and the contact, and what not to do (e.g. "don't let anyone open the motor before
the company sees it").

Who decides what:
- Reading a document: Claude Fable 5.2 (PDF, photo or text in; JSON schema out, with an exact quote from
  the document for every field it fills) when ANTHROPIC_API_KEY is set. Offline, or without a key, a rule
  reader handles text and text PDFs (labelled lines such as "Valid till:", "Covers:", "Helpline:"). A photo
  without a key is stored and marked "needs a key to read".
- The coverage verdict: plain code below, never a language model. Dates are compared, the fault's words
  are looked up in the document's covers / excludes lists, and specific wording beats general wording.
  Nothing on the alert is model-generated: every name, number and step comes from the owner's own papers.
- The verdict is advice: "likely claimable" is not a promise. The alert says to confirm with the company.

Storage: <node db folder>/machine_docs/ (git-ignored): the original files and index.json. The sample papers
in data/sample_docs/<machine>/ are indexed on first use (DHADKAN_SAMPLE_DOCS=0 turns that off) so the demo
machines (Loom 1 = A-G1, Pump 1 = A-P1, Loom 1 at Gupta = B-G1) have papers out of the box.
"""

import base64
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import costs, diagnosis
from .config import DATA, ROOT
from .notify import env

MODEL = "claude-fable-5-2"
BETAS = ["server-side-fallback-2026-07-01"]
SAMPLES = DATA / "sample_docs"
LOG = ROOT / "data" / "llm" / "calls.jsonl"
MAX_BYTES = 15 * 1024 * 1024
KINDS = ("warranty", "insurance", "amc", "invoice", "manual", "other")
COVER_KINDS = ("warranty", "amc", "insurance")  # the papers that can pay for a repair
MIME = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".txt": "text/plain", ".md": "text/plain"}  # fmt: skip
IST = timezone(timedelta(hours=5, minutes=30))
PRICE_USD_PER_MTOK = {"input": 8.0, "output": 40.0, "cache_read": 0.20, "cache_write_5m": 10.0}

# The words a document uses for each fault in server/faults.yaml. `gradual` faults are what insurers call
# wear and tear; a sudden breakdown (a burnt winding) is what a machinery breakdown policy pays for.
FAULT_TERMS: dict[str, dict[str, Any]] = {
    "bearing_wear": {"terms": ["bearing"], "gradual": True},
    "misalignment": {"terms": ["alignment", "coupling", "shaft"], "gradual": True},
    "imbalance": {"terms": ["imbalance", "balancing", "impeller", "fan blade", "rotor"], "gradual": True},
    "belt_slip": {"terms": ["belt"], "gradual": True},
    "cavitation": {"terms": ["cavitation", "impeller", "mechanical seal", "foot valve"], "gradual": True},
    "motor_winding": {"terms": ["winding", "burn-out", "burnout", "burn out", "burnt", "rewind"], "gradual": False},
    "looseness": {"terms": ["foundation", "mounting", "base bolt", "anti-vibration mount"], "gradual": True},
}  # fmt: skip
WEAR = ("wear and tear", "wear & tear", "gradual", "deterioration", "normal wear")
GENERIC_COVER = ("manufacturing defect", "all parts", "any part", "sudden and unforeseen", "accidental",
                 "breakdown of any", "mechanical and electrical breakdown")  # fmt: skip
DONT_OPEN = re.compile(r"unauthori[sz]ed|opened by|tamper|seal (is )?broken|dismantl", re.I)
COST_BASIS_FAULT = {"rewind": "motor_winding"}  # what "if ignored" turns into, for the breakdown cover


# ---------------------------------------------------------------- reading a document (rules)
FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "provider": ("manufacturer", "insurer", "insurance company", "issued by", "seller", "dealer", "company",
                 "service provider", "warrantor"),
    "number": ("policy no", "policy number", "warranty no", "warranty card no", "card no", "certificate no",
               "invoice no", "amc no", "contract no"),
    "serial_no": ("serial no", "machine serial no", "machine sr. no", "sr. no", "motor serial no", "serial number"),
    "machine": ("machine", "product", "model", "equipment", "item"),
    "start": ("date of purchase", "purchase date", "invoice date", "valid from", "start date",
              "policy period from", "period from", "commencement date", "date of installation"),
    "end": ("valid till", "valid until", "valid up to", "warranty up to", "expiry date", "expires on",
            "end date", "policy period to", "period to"),
    "period": ("warranty period", "period of warranty", "duration"),
    "deductible_inr": ("deductible", "excess", "compulsory deductible"),
    "max_claim_inr": ("sum insured", "max claim", "maximum claim", "limit of liability"),
    "claim_within": ("intimate within", "report within", "claim within", "notify within", "intimation"),
    "phone": ("helpline", "toll free", "phone", "claims helpline", "customer care", "contact no", "mobile",
              "whatsapp"),
    "email": ("email", "e-mail", "claims email"),
    "contact_name": ("contact person", "claims contact", "service engineer", "contact"),
    "covers": ("covers", "covered", "what is covered", "scope of cover", "coverage", "warranty covers"),
    "excludes": ("not covered", "exclusions", "excludes", "what is not covered", "excluded"),
    "conditions": ("conditions", "warranty void if", "void if", "important conditions", "terms"),
    "claim_steps": ("how to claim", "claim procedure", "claim process", "steps to claim", "to make a claim"),
}  # fmt: skip
LIST_FIELDS = ("covers", "excludes", "conditions", "claim_steps")
DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d-%b-%Y", "%b %d, %Y",
                "%B %d, %Y", "%d %b, %Y")  # fmt: skip


def parse_date(s: str | None) -> str | None:
    """'12-08-2025', '12 Aug 2025', '2025-08-12' -> '2025-08-12' (day first, as Indian papers write it)."""
    if not s:
        return None
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s.strip().rstrip(".,"))
    m = re.search(r"\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[ -][A-Za-z]{3,9},? \d{4}|"
                  r"[A-Za-z]{3,9} \d{1,2}, \d{4}", s)  # fmt: skip
    if not m:
        return None
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(m.group(0), f).date().isoformat()
        except ValueError:
            continue
    return None


def parse_inr(s: str | None) -> int | None:
    """'Rs. 5,000 per claim' / '₹1,50,000' / 'INR 2500' -> 5000 / 150000 / 2500."""
    if not s:
        return None
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(lakh|lac)?", s.replace("₹", " "), re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return round(v * 100000) if m.group(2) else round(v)


def parse_hours(s: str | None) -> int | None:
    """'48 hours' -> 48, '7 days' -> 168, '2 weeks' -> 336."""
    if not s:
        return None
    m = re.search(r"(\d+)\s*(hour|hr|day|week|month)", s, re.I)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * {"hour": 1, "hr": 1, "day": 24, "week": 168, "month": 720}[unit]


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    day = min(d.day, [31, 29 if (d.year + y) % 4 == 0 else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m])
    return date(d.year + y, m + 1, day) - timedelta(days=1)


def guess_kind(text: str, filename: str = "") -> str:
    t = (filename + "\n" + text[:1500]).lower()
    for kind, words in (("insurance", ("insurance", "policy schedule", "insurer", "sum insured")),
                        ("amc", ("annual maintenance contract", "amc")),
                        ("warranty", ("warranty", "guarantee")),
                        ("invoice", ("tax invoice", "invoice", "bill of supply")),
                        ("manual", ("manual", "operating instructions", "user guide"))):  # fmt: skip
        if any(w in t for w in words):
            return kind
    return "other"


def _label(line: str) -> tuple[str | None, str]:
    """'Valid till: 11-08-2027' -> ('end', '11-08-2027'); the longest matching label wins."""
    if ":" not in line:
        return None, line
    head, _, value = line.partition(":")
    h = re.sub(r"[^a-z. &-]", "", head.lower()).strip()
    best: tuple[int, str | None] = (0, None)
    for field, labels in FIELD_LABELS.items():
        for lab in labels:
            if (h == lab or h.startswith(lab + " ") or h.endswith(" " + lab)) and len(lab) > best[0]:
                best = (len(lab), field)
    return best[1], value.strip()


def _items(value: str) -> list[str]:
    parts = re.split(r";|\s+\|\s+|(?<=[a-z\)]),\s+(?=[A-Za-z])", value)
    return [p.strip(" .-•") for p in parts if p.strip(" .-•")]


def read_rules(text: str, filename: str = "") -> dict[str, Any]:
    """Fields from a text document with labelled lines and bullet lists. Returns {} for fields not found."""
    out: dict[str, Any] = {k: [] for k in LIST_FIELDS}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        bullet = re.match(r"^(?:[-•*]|\d+[.)])\s+(.*)", line)
        if bullet and current:
            out[current].append(bullet.group(1).strip())
            continue
        field, value = _label(line)
        if field is None:
            if not line.endswith(":"):
                current = None
            continue
        if field in LIST_FIELDS:
            current = field
            if value:
                out[field].extend(_items(value))
            continue
        current = None
        if value and field not in out:
            out[field] = value
    f: dict[str, Any] = {k: out[k] for k in LIST_FIELDS}
    f["provider"] = out.get("provider")
    f["number"] = out.get("number")
    f["serial_no"] = out.get("serial_no")
    f["machine"] = out.get("machine")
    f["start"] = parse_date(out.get("start"))
    f["end"] = parse_date(out.get("end"))
    if f["end"] is None and f["start"] and out.get("period"):
        m = re.search(r"(\d+)\s*(month|year)", out["period"], re.I)
        if m:
            months = int(m.group(1)) * (12 if m.group(2).lower().startswith("year") else 1)
            f["end"] = add_months(date.fromisoformat(f["start"]), months).isoformat()
    f["deductible_inr"] = parse_inr(out.get("deductible_inr"))
    f["max_claim_inr"] = parse_inr(out.get("max_claim_inr"))
    f["claim_within_hours"] = parse_hours(out.get("claim_within"))
    if f["claim_within_hours"] is None:  # "... within 48 hours" in a step or condition
        for s in f["claim_steps"] + f["conditions"]:  # the owner's deadline, not the company's visit time
            if re.search(
                r"(report|intimat|notif|inform|tell|claim|call)\w*\b.*within\s+\d+\s*(hour|hr|day)", s, re.I
            ):
                f["claim_within_hours"] = parse_hours(s)
                break
    f["contact"] = {"name": out.get("contact_name"), "phone": out.get("phone"), "email": out.get("email")}
    f["kind"] = guess_kind(text, filename)
    return f


# ---------------------------------------------------------------- reading a document (Claude)
EXTRACT_SYSTEM = (
    "You read one paper belonging to a machine in a small Indian factory: a warranty card, an insurance policy "
    "schedule, an invoice, an AMC contract, a manual, or something else. Copy out the facts a factory owner "
    "needs to claim a repair. Use only what is written in the document; leave a field null (or an empty list) "
    "when the document does not say it. Never guess a date, number, phone or amount. Dates as YYYY-MM-DD "
    "(Indian papers write day first). If only a warranty period is given (e.g. 24 months from purchase), "
    "compute the end date from the purchase date. Keep list items short, in the document's own words, in "
    "English. For every non-empty field, put an exact short quote from the document in `quotes`."
)


def extract_schema() -> dict[str, Any]:
    s, n, i = (
        {"type": ["string", "null"]},
        {"type": ["integer", "null"]},
        {"type": "array", "items": {"type": "string"}},
    )
    props = {
        "kind": {"type": "string", "enum": list(KINDS)}, "provider": s, "number": s, "serial_no": s, "machine": s,
        "start": s, "end": s, "covers": i, "excludes": i, "conditions": i, "claim_steps": i,
        "deductible_inr": n, "max_claim_inr": n, "claim_within_hours": n,
        "contact": {"type": "object", "properties": {"name": s, "phone": s, "email": s},
                    "required": ["name", "phone", "email"], "additionalProperties": False},
        "quotes": {"type": "array", "items": {
            "type": "object", "properties": {"field": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["field", "quote"], "additionalProperties": False}},
    }  # fmt: skip
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def build_extract_request(data: bytes, mime: str, filename: str, machine: dict[str, Any]) -> dict[str, Any]:
    """Keyword arguments for client.beta.messages.stream(...): the document first, then the question."""
    if mime == "application/pdf":
        block: dict[str, Any] = {"type": "document", "title": filename,
                                 "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}  # fmt: skip
    elif mime.startswith("image/"):
        block = {
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()},
        }
    else:
        block = {"type": "document", "title": filename,
                 "source": {"type": "text", "media_type": "text/plain", "data": data.decode("utf-8", "replace")}}  # fmt: skip
    about = {k: machine.get(k) for k in ("name", "type", "motor_hp")}
    return {
        "model": MODEL,
        "max_tokens": 16000,
        "betas": BETAS,
        "fallbacks": "default",
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": extract_schema()}},
        "system": [{"type": "text", "text": EXTRACT_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": [
            block,
            {"type": "text", "text": f"File name: {filename}. It was uploaded for this machine: "
                                     f"{json.dumps(about, ensure_ascii=False)}. Extract the fields."},
        ]}],
    }  # fmt: skip


def _log(row: dict[str, Any]) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _cost(usage: Any) -> dict[str, float]:
    def get(k: str) -> float:
        return float(getattr(usage, k, 0) or 0)

    usd = (get("input_tokens") * PRICE_USD_PER_MTOK["input"] + get("output_tokens") * PRICE_USD_PER_MTOK["output"]
           + get("cache_read_input_tokens") * PRICE_USD_PER_MTOK["cache_read"]
           + get("cache_creation_input_tokens") * PRICE_USD_PER_MTOK["cache_write_5m"]) / 1e6  # fmt: skip
    return {"usd": round(usd, 5), "inr": round(usd * costs.load()["usd_to_inr"], 3)}


def read_claude(request: dict[str, Any], client: Any = None) -> dict[str, Any] | None:
    """Claude's reading, or None (no key, offline, refusal, bad output): the caller falls back to the rules."""
    if client is None:
        key = env.get("ANTHROPIC_API_KEY")
        if not key or env.get("DHADKAN_LLM") == "0":
            return None
        import anthropic

        client = anthropic.Anthropic(api_key=key)
    t0 = time.time()
    try:
        with client.beta.messages.stream(**request) as stream:
            msg = stream.get_final_message()
    except Exception as e:  # network down, rate limit, bad request: the rules still work
        _log({"ts": t0, "task": "read_document", "model": request["model"], "error": type(e).__name__})
        return None
    _log({"ts": t0, "task": "read_document", "model": request["model"], "served_by": getattr(msg, "model", None),
          "stop_reason": msg.stop_reason, "seconds": round(time.time() - t0, 2), **_cost(msg.usage)})  # fmt: skip
    if msg.stop_reason in ("refusal", "max_tokens"):
        return None
    try:
        text = next(b.text for b in msg.content if getattr(b, "type", None) == "text")
        out = json.loads(text)
        assert set(extract_schema()["required"]) <= set(out)
    except Exception:
        return None
    out["start"], out["end"] = parse_date(out.get("start")), parse_date(out.get("end"))  # trust, but check
    return out


def pdf_text(data: bytes) -> str | None:
    """Text of a text PDF (pypdf, optional). Scanned PDFs have none: those need Claude."""
    try:
        import io

        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages).strip() or None
    except Exception:
        return None


def read_document(data: bytes, filename: str, machine: dict[str, Any], client: Any = None,
                  use_claude: bool = True) -> dict[str, Any]:  # fmt: skip
    """-> {"fields": ..., "read_by": "claude" | "rules" | "none", "text": plain text if any, "warnings": [...]}"""
    mime = MIME.get(Path(filename).suffix.lower(), "application/octet-stream")
    text = (
        data.decode("utf-8", "replace")
        if mime == "text/plain"
        else pdf_text(data) if mime == "application/pdf" else None
    )
    warnings: list[str] = []
    if use_claude:
        got = read_claude(build_extract_request(data, mime, filename, machine), client)
        if got is not None:
            return {"fields": got, "read_by": "claude", "text": text, "warnings": warnings}
    if text:
        return {"fields": read_rules(text, filename), "read_by": "rules", "text": text, "warnings": warnings}
    warnings.append("Stored, but not read: a photo or scanned PDF needs ANTHROPIC_API_KEY (Claude reads it).")
    return {"fields": {**read_rules("", filename), "kind": guess_kind("", filename)}, "read_by": "none",
            "text": None, "warnings": warnings}  # fmt: skip


# ---------------------------------------------------------------- the store
class DocStore:
    """Per-machine papers on disk: <root>/<machine>/<doc id><ext> and <root>/index.json."""

    def __init__(self, root: Path, samples: Path | None = SAMPLES) -> None:
        self.root, self.samples = Path(root), samples
        self.lock = threading.Lock()
        self.index_path = self.root / "index.json"

    def _load(self) -> dict[str, Any]:
        if self.index_path.exists():
            try:
                return json.loads(self.index_path.read_text(encoding="utf-8"))
            except ValueError:
                pass
        return {"docs": [], "seeded": []}

    def _save(self, idx: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.index_path)

    def seed(self, mid: str, machine: dict[str, Any]) -> None:
        """Index the sample papers of a demo machine once (rules only: same result with or without a key)."""
        if env.get("DHADKAN_SAMPLE_DOCS") == "0" or self.samples is None:
            return
        with self.lock:
            idx = self._load()
            if mid in idx["seeded"]:
                return
            idx["seeded"].append(mid)
            self._save(idx)
        folder = self.samples / mid
        if folder.is_dir():
            for p in sorted(folder.iterdir()):
                if p.suffix.lower() in MIME:
                    self.add(mid, machine, p.read_bytes(), p.name, sample=True, use_claude=False)

    def add(self, mid: str, machine: dict[str, Any], data: bytes, filename: str, kind: str | None = None,
            sample: bool = False, client: Any = None, use_claude: bool = True) -> dict[str, Any]:  # fmt: skip
        if len(data) > MAX_BYTES:
            raise ValueError(f"file too large (max {MAX_BYTES // (1024 * 1024)} MB)")
        ext = Path(filename).suffix.lower()
        if ext not in MIME:
            raise ValueError(f"unsupported file type {ext or '(none)'}: use PDF, JPG, PNG, WEBP or TXT")
        name = re.sub(r"[^\w.\- ]", "_", Path(filename).name)[:120]
        sha = hashlib.sha256(data).hexdigest()
        with self.lock:
            dup = next((d for d in self._load()["docs"] if d["machine"] == mid and d["sha256"] == sha), None)
        if dup:
            return dup
        got = read_document(data, name, machine, client, use_claude)
        fields = got["fields"]
        if kind in KINDS:
            fields["kind"] = kind
        doc = {"id": uuid.uuid4().hex[:10], "machine": mid, "filename": name, "kind": fields.get("kind") or "other",
               "sha256": sha, "bytes": len(data), "uploaded_at": datetime.now(IST).isoformat(timespec="seconds"),
               "read_by": got["read_by"], "sample": sample, "fields": fields, "warnings": got["warnings"],
               "text": (got["text"] or "")[:20000]}  # fmt: skip
        folder = self.root / mid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{doc['id']}{ext}").write_bytes(data)
        with self.lock:
            idx = self._load()
            idx["docs"].append(doc)
            self._save(idx)
        return doc

    def list(self, mid: str) -> list[dict[str, Any]]:
        with self.lock:
            return [d for d in self._load()["docs"] if d["machine"] == mid]

    def get(self, mid: str, doc_id: str) -> dict[str, Any] | None:
        return next((d for d in self.list(mid) if d["id"] == doc_id), None)

    def file(self, mid: str, doc_id: str) -> Path | None:
        d = self.get(mid, doc_id)
        if d is None:
            return None
        p = self.root / mid / f"{doc_id}{Path(d['filename']).suffix.lower()}"
        return p if p.is_file() else None

    def delete(self, mid: str, doc_id: str) -> bool:
        p = self.file(mid, doc_id)
        with self.lock:
            idx = self._load()
            n = len(idx["docs"])
            idx["docs"] = [d for d in idx["docs"] if not (d["machine"] == mid and d["id"] == doc_id)]
            self._save(idx)
        if p:
            p.unlink(missing_ok=True)
        return len(idx["docs"]) < n


# ---------------------------------------------------------------- the verdict (code, not a model)
def today_ist() -> date:
    return datetime.now(IST).date()


def _hit(items: list[str], words: list[str] | tuple[str, ...]) -> str | None:
    for it in items:
        low = it.lower()
        if any(re.search(r"\b" + re.escape(w), low) for w in words):
            return it
    return None


def judge(doc: dict[str, Any], fault: str, on: date) -> dict[str, Any]:
    """One paper vs one fault -> covered / conditional / not_mentioned / excluded / expired / not_started."""
    f = doc["fields"]
    spec = FAULT_TERMS.get(fault, {"terms": [fault.replace("_", " ")], "gradual": True})
    terms = spec["terms"]
    out: dict[str, Any] = {"doc": doc["id"], "kind": doc["kind"], "filename": doc["filename"]}
    end = date.fromisoformat(f["end"]) if f.get("end") else None
    start = date.fromisoformat(f["start"]) if f.get("start") else None
    if end and on > end:
        return {**out, "status": "expired", "why": f"ended on {end.isoformat()}"}
    if start and on < start:
        return {**out, "status": "not_started", "why": f"starts on {start.isoformat()}"}
    covers, excludes = f.get("covers") or [], f.get("excludes") or []
    if (x := _hit(excludes, terms)) is not None:  # specific exclusion beats everything
        return {**out, "status": "excluded", "why": x}
    if (c := _hit(covers, terms)) is not None:  # specific cover beats a general exclusion
        if re.search(
            r"\bdefect", c, re.I
        ):  # "winding failure due to a manufacturing defect": only if it is one
            return {**out, "status": "conditional", "why": c}
        return {**out, "status": "covered", "why": c, "dates_known": end is not None}
    if spec["gradual"] and (x := _hit(excludes, WEAR)) is not None:
        return {**out, "status": "excluded", "why": x}
    if (c := _hit(covers, GENERIC_COVER)) is not None:
        return {**out, "status": "conditional", "why": c}
    return {**out, "status": "not_mentioned", "why": "this fault is not named in the paper"}


VERDICT_RANK = {
    "covered": 0,
    "conditional": 1,
    "not_mentioned": 2,
    "excluded": 3,
    "not_started": 4,
    "expired": 5,
}
KIND_RANK = {"warranty": 0, "amc": 1, "insurance": 2}  # no deductible first


def check(store: DocStore, mid: str, machine: dict[str, Any], fault: str | None,
          fix_inr: list[int] | None = None, on: date | None = None) -> dict[str, Any]:  # fmt: skip
    """The machine's papers vs the fault the diagnosis names. verdict: claimable | check | not_covered | no_docs."""
    store.seed(mid, machine)
    on = on or today_ist()
    docs = store.list(mid)
    paying = [d for d in docs if d["kind"] in COVER_KINDS]
    base: dict[str, Any] = {"machine": mid, "fault": fault, "on": on.isoformat(), "documents": len(docs),
                            "serial_no": next((d["fields"].get("serial_no") for d in docs if d["fields"].get("serial_no")), None)}  # fmt: skip
    if not paying:
        return {**base, "verdict": "no_docs", "judgements": []}
    if fault is None:  # nothing named yet: just what is in force
        live_docs = [
            d for d in paying if not d["fields"].get("end") or date.fromisoformat(d["fields"]["end"]) >= on
        ]
        return {**base, "verdict": "unknown_fault", "judgements": [],
                "in_force": [_summary(d, on) for d in live_docs]}  # fmt: skip
    js = sorted((judge(d, fault, on) for d in paying),
                key=lambda j: (VERDICT_RANK[j["status"]], KIND_RANK.get(j["kind"], 9)))  # fmt: skip
    best = js[0]
    verdict = {"covered": "claimable", "conditional": "check", "not_mentioned": "check"}.get(
        best["status"], "not_covered"
    )
    doc = next(d for d in paying if d["id"] == best["doc"])
    out = {**base, "verdict": verdict, "judgements": js, "best": {**best, **_summary(doc, on)}}
    kb = diagnosis.load()["faults"].get(fault, {})
    voids = [v["en"] for v in (kb.get("warranty", {}).get("voided_by") or [])]
    conds = list(doc["fields"].get("conditions") or [])
    out["conditions"] = conds + [v for v in voids if v not in conds]
    out["dont_open"] = any(DONT_OPEN.search(c) for c in conds)
    # what the claim saves: a warranty/AMC pays the whole repair; a policy pays above its deductible
    if fix_inr and verdict != "not_covered":
        ded = doc["fields"].get("deductible_inr") or 0
        lo, hi = max(0, fix_inr[0] - ded), max(0, fix_inr[1] - ded)
        out["saves_inr"] = [lo, hi]
        out["below_deductible"] = bool(ded and hi == 0)
    # if the owner waits and the motor burns: is that breakdown covered? (insurance usually yes)
    nxt = COST_BASIS_FAULT.get((kb.get("if_ignored") or {}).get("cost_basis", ""))
    if nxt and nxt != fault:
        # a breakdown after the owner was warned is the insurer's business, not a manufacturing defect
        later = sorted((judge(d, nxt, on) for d in paying),
                       key=lambda j: (VERDICT_RANK[j["status"]], j["kind"] != "insurance"))[0]  # fmt: skip
        if later["status"] == "covered":
            d2 = next(d for d in paying if d["id"] == later["doc"])
            out["if_ignored"] = {"fault": nxt, **later, **_summary(d2, on)}
    return out


def _summary(doc: dict[str, Any], on: date) -> dict[str, Any]:
    f = doc["fields"]
    end = f.get("end")
    return {"doc": doc["id"], "kind": doc["kind"], "filename": doc["filename"], "provider": f.get("provider"),
            "number": f.get("number"), "start": f.get("start"), "end": end,
            "days_left": (date.fromisoformat(end) - on).days if end else None,
            "deductible_inr": f.get("deductible_inr"), "claim_within_hours": f.get("claim_within_hours"),
            "claim_steps": f.get("claim_steps") or [], "contact": f.get("contact") or {}}  # fmt: skip


# ---------------------------------------------------------------- words for the owner
def _date(iso: str | None, lang: str) -> str:
    if not iso:
        return "?"
    d = date.fromisoformat(iso)
    return d.strftime("%d-%m-%Y")


def _hours(h: int | None, lang: str) -> str:
    if not h:
        return ""
    if h % 24 == 0 and h >= 48:
        n, u = h // 24, {"hi": "दिन", "mr": "दिवसांत", "gu": "દિવસમાં", "hi_latn": "din", "en": "days"}[lang]
    else:
        n, u = h, {"hi": "घंटे", "mr": "तासांत", "gu": "કલાકમાં", "hi_latn": "ghante", "en": "hours"}[lang]
    return f"{n} {u}"


KIND_WORD = {
    "warranty": {"en": "warranty", "hi": "वारंटी", "mr": "वॉरंटी", "hi_latn": "warranty", "gu": "વોરંટી"},
    "amc": {"en": "AMC", "hi": "AMC", "mr": "AMC", "hi_latn": "AMC", "gu": "AMC"},
    "insurance": {"en": "insurance", "hi": "बीमा", "mr": "विमा", "hi_latn": "bima", "gu": "વીમો"},
}
LINES: dict[str, dict[str, str]] = {
    "claimable": {
        "en": "✅ {kind} ({who}, valid till {end}) covers this: likely claimable{save}.",
        "hi": "✅ {kind} ({who}, {end} तक) में यह कवर है: क्लेम हो सकता है{save}।",
        "mr": "✅ {kind} ({who}, {end} पर्यंत) मध्ये हे कव्हर आहे: क्लेम होऊ शकतो{save}.",
        "hi_latn": "✅ {kind} ({who}, {end} tak) mein yeh cover hai: claim ho sakta hai{save}.",
        "gu": "✅ {kind} ({who}, {end} સુધી) માં આ કવર છે: ક્લેમ થઈ શકે{save}.",
    },
    "check": {
        "en": "❔ {kind} ({who}, valid till {end}) is active but does not clearly name this fault: ask them before repair.",
        "hi": "❔ {kind} ({who}, {end} तक) चालू है, पर यह खराबी साफ़ लिखी नहीं: मरम्मत से पहले पूछें।",
        "mr": "❔ {kind} ({who}, {end} पर्यंत) चालू आहे, पण हा बिघाड स्पष्ट लिहिलेला नाही: दुरुस्तीआधी विचारा.",
        "hi_latn": "❔ {kind} ({who}, {end} tak) chalu hai, par yeh kharabi saaf likhi nahi: marammat se pehle poochhein.",
        "gu": "❔ {kind} ({who}, {end} સુધી) ચાલુ છે, પણ આ ખામી સ્પષ્ટ લખી નથી: રિપેર પહેલાં પૂછો.",
    },
    "not_covered": {
        "en": "❌ Not covered by this machine's papers ({why}).",
        "hi": "❌ इस मशीन के कागज़ों में यह कवर नहीं ({why})।",
        "mr": "❌ या मशीनच्या कागदपत्रांत हे कव्हर नाही ({why}).",
        "hi_latn": "❌ Is machine ke kaagazon mein yeh cover nahi ({why}).",
        "gu": "❌ આ મશીનના કાગળોમાં આ કવર નથી ({why}).",
    },
    "no_docs": {
        "en": "📄 No warranty or insurance papers on file for this machine: upload them and I will check every alert.",
        "hi": "📄 इस मशीन के वारंटी/बीमा कागज़ नहीं मिले: अपलोड करें, हर अलर्ट पर मैं जाँच दूँगा।",
        "mr": "📄 या मशीनची वॉरंटी/विमा कागदपत्रे नाहीत: अपलोड करा, प्रत्येक अलर्टवर मी तपासेन.",
        "hi_latn": "📄 Is machine ke warranty/bima kaagaz nahi mile: upload karein, har alert par main check karunga.",
        "gu": "📄 આ મશીનના વોરંટી/વીમા કાગળ નથી: અપલોડ કરો, દરેક એલર્ટ પર હું તપાસીશ.",
    },
    "call": {
        "en": " Call {phone}{within} before any repair.",
        "hi": " मरम्मत से पहले {phone} पर बताएं{within}।",
        "mr": " दुरुस्तीआधी {phone} वर कळवा{within}.",
        "hi_latn": " Marammat se pehle {phone} par batayein{within}.",
        "gu": " રિપેર પહેલાં {phone} પર જાણ કરો{within}.",
    },
    "within": {"en": " (within {h})", "hi": " ({h} के अंदर)", "mr": " ({h})", "hi_latn": " ({h} ke andar)", "gu": " ({h})"},
    "dont_open": {
        "en": " Don't let anyone open the machine before the company sees it, or the claim can be refused.",
        "hi": " कंपनी के देखने से पहले मशीन किसी से न खुलवाएँ, वरना क्लेम रद्द हो सकता है।",
        "mr": " कंपनीने पाहण्याआधी मशीन कुणाकडूनही उघडू नका, नाहीतर क्लेम नाकारला जाऊ शकतो.",
        "hi_latn": " Company ke dekhne se pehle machine kisi se na khulwayein, warna claim radd ho sakta hai.",
        "gu": " કંપની જુએ તે પહેલાં મશીન કોઈ પાસે ન ખોલાવો, નહીં તો ક્લેમ રદ થઈ શકે.",
    },
    "save_upto": {"en": ", saves up to {amt}", "hi": ", {amt} तक बचेंगे", "mr": ", {amt} पर्यंत वाचतील",
                  "hi_latn": ", {amt} tak bachenge", "gu": ", {amt} સુધી બચશે"},
    "save": {"en": ", saves ~{amt}", "hi": ", ~{amt} बचेंगे", "mr": ", ~{amt} वाचतील", "hi_latn": ", ~{amt} bachenge", "gu": ", ~{amt} બચશે"},
    "ded": {"en": " after the {amt} deductible", "hi": " ({amt} कटौती के बाद)", "mr": " ({amt} वजावटीनंतर)",
            "hi_latn": " ({amt} katauti ke baad)", "gu": " ({amt} કપાત પછી)"},
    "below_ded": {
        "en": " The repair costs less than the {amt} deductible, so a claim won't pay: repair it now.",
        "hi": " मरम्मत {amt} कटौती से सस्ती है, क्लेम से पैसा नहीं मिलेगा: अभी ठीक कराएँ।",
        "mr": " दुरुस्ती {amt} वजावटीपेक्षा स्वस्त आहे, क्लेममधून पैसे मिळणार नाहीत: आत्ता दुरुस्त करा.",
        "hi_latn": " Marammat {amt} katauti se sasti hai, claim se paisa nahi milega: abhi theek karayein.",
        "gu": " રિપેર {amt} કપાત કરતાં સસ્તું છે, ક્લેમથી પૈસા નહીં મળે: હમણાં રિપેર કરાવો.",
    },
    "later": {
        "en": " If it is left and the motor burns, {kind} ({who}) covers that breakdown{ded}.",
        "hi": " छोड़ दिया और मोटर जल गई तो वह ब्रेकडाउन {kind} ({who}) में कवर है{ded}।",
        "mr": " दुर्लक्ष केले आणि मोटर जळली तर तो ब्रेकडाउन {kind} ({who}) मध्ये कव्हर आहे{ded}.",
        "hi_latn": " Chhod diya aur motor jal gayi to woh breakdown {kind} ({who}) mein cover hai{ded}.",
        "gu": " છોડી દીધું અને મોટર બળી ગઈ તો તે બ્રેકડાઉન {kind} ({who}) માં કવર છે{ded}.",
    },
    "in_force": {
        "en": "📄 Papers in force: {papers}.", "hi": "📄 चालू कागज़: {papers}।", "mr": "📄 चालू कागदपत्रे: {papers}.",
        "hi_latn": "📄 Chalu kaagaz: {papers}.", "gu": "📄 ચાલુ કાગળ: {papers}.",
    },
    "claim_reply": {
        "en": " Reply 4 = claim help.", "hi": " 4 = क्लेम मदद।", "mr": " 4 = क्लेम मदत.",
        "hi_latn": " 4 = claim madad.", "gu": " 4 = ક્લેમ મદદ.",
    },
}  # fmt: skip
WHY_WORDS = {"expired": {"en": "expired", "hi": "खत्म", "mr": "संपली", "hi_latn": "khatam", "gu": "પૂરી"}}


def _rs(v: int) -> str:
    from .owner_agent import inr

    return inr(v, words=False)


def line(cov: dict[str, Any], lang: str, reply_hint: bool = False) -> str:
    """One short WhatsApp sentence (plus whom to call and what not to do) in the owner's language."""
    lang = lang if lang in LINES["claimable"] else "en"
    v = cov.get("verdict")
    if v == "unknown_fault" and cov.get("in_force"):
        till = {"en": "till {d}", "hi": "{d} तक", "mr": "{d} पर्यंत", "hi_latn": "{d} tak", "gu": "{d} સુધી"}[
            lang
        ]
        papers = "; ".join(
            f"{KIND_WORD.get(d['kind'], KIND_WORD['warranty'])[lang]} ({d.get('provider') or d['filename']}"
            + (", " + till.format(d=_date(d["end"], lang)) if d.get("end") else "")
            + ")"
            for d in cov["in_force"]
        )
        return LINES["in_force"][lang].format(papers=papers)
    if v in (None, "unknown_fault", "error"):
        return ""
    if v == "no_docs":
        return LINES["no_docs"][lang]
    b = cov["best"]
    kind = KIND_WORD.get(b["kind"], KIND_WORD["warranty"])[lang]
    who = b.get("provider") or b["filename"]
    if v == "not_covered":
        why = b["why"]
        if b["status"] == "expired":
            why = f"{kind} {WHY_WORDS['expired'][lang]} {_date(b.get('end'), lang)}"
        return LINES["not_covered"][lang].format(why=why)
    save = ""
    if v == "claimable" and cov.get("saves_inr") and cov["saves_inr"][1] > 0:
        lo, hi = cov["saves_inr"]
        if lo == 0:
            save = LINES["save_upto"][lang].format(amt=_rs(hi))
        else:
            save = LINES["save"][lang].format(amt=_rs(hi) if lo == hi else f"{_rs(lo)}-{_rs(hi)}")
        if b.get("deductible_inr"):
            save += LINES["ded"][lang].format(amt=_rs(b["deductible_inr"]))
    out = LINES[v][lang].format(
        kind=kind[:1].upper() + kind[1:], who=who, end=_date(b.get("end"), lang), save=save
    )
    if cov.get("below_deductible"):
        out += LINES["below_ded"][lang].format(amt=_rs(b["deductible_inr"]))
        return out
    phone = (b.get("contact") or {}).get("phone")
    if phone:
        h = _hours(b.get("claim_within_hours"), lang)
        out += LINES["call"][lang].format(phone=phone, within=LINES["within"][lang].format(h=h) if h else "")
    if cov.get("dont_open"):
        out += LINES["dont_open"][lang]
    if reply_hint:
        out += LINES["claim_reply"][lang]
    return out


def alert_text(cov: dict[str, Any], lang: str) -> str:
    """What the WhatsApp alert adds: the verdict, the breakdown cover if the owner waits, and '4 = claim help'."""
    lang = lang if lang in LINES["claimable"] else "en"
    out = line(cov, lang) + later_line(cov, lang)
    if cov.get("verdict") in ("claimable", "check") and not cov.get("below_deductible"):
        out += LINES["claim_reply"][lang]
    return out.strip()


def later_line(cov: dict[str, Any], lang: str) -> str:
    """'If you wait and the motor burns, the insurance pays for that' (only when a paper says so)."""
    lang = lang if lang in LINES["later"] else "en"
    nx = cov.get("if_ignored")
    if not nx:
        return ""
    kind = KIND_WORD.get(nx["kind"], KIND_WORD["insurance"])[lang]
    ded = LINES["ded"][lang].format(amt=_rs(nx["deductible_inr"])) if nx.get("deductible_inr") else ""
    return LINES["later"][lang].format(kind=kind, who=nx.get("provider") or nx["filename"], ded=ded)


CLAIM_HEAD = {
    "en": "Claim help for {m}:", "hi": "{m} के लिए क्लेम मदद:", "mr": "{m} साठी क्लेम मदत:",
    "hi_latn": "{m} ke liye claim madad:", "gu": "{m} માટે ક્લેમ મદદ:",
}  # fmt: skip
CLAIM_LABELS = {
    "en": ("Steps", "Contact", "Keep ready", "Letter to send (English)"),
    "hi": ("कदम", "संपर्क", "तैयार रखें", "भेजने के लिए पत्र (अंग्रेज़ी)"),
    "mr": ("पायऱ्या", "संपर्क", "तयार ठेवा", "पाठवण्यासाठी पत्र (इंग्रजी)"),
    "hi_latn": ("Kadam", "Sampark", "Taiyaar rakhein", "Bhejne ke liye patra (English)"),
    "gu": ("પગલાં", "સંપર્ક", "તૈયાર રાખો", "મોકલવા માટે પત્ર (અંગ્રેજી)"),
}  # fmt: skip


def claim_help(cov: dict[str, Any], machine: dict[str, Any], lang: str, fault_name: str | None,
               evidence: str = "", owner: str | None = None, factory: str | None = None) -> str:  # fmt: skip
    """Reply '4': steps, contact, what to keep ready, and a claim letter in English ready to forward."""
    lang = lang if lang in CLAIM_HEAD else "en"
    name = (
        machine.get("name_local") if lang in ("hi", "mr") and machine.get("name_local") else machine["name"]
    )
    head = [CLAIM_HEAD[lang].format(m=name), line(cov, lang)]
    if cov.get("verdict") not in ("claimable", "check"):
        return "\n".join(x for x in head if x)
    b = cov["best"]
    L = CLAIM_LABELS[lang]
    steps = b.get("claim_steps") or []
    c = b.get("contact") or {}
    keep = [x for x in (b.get("number") and f"{b['kind']} no. {b['number']}",
                        cov.get("serial_no") and f"serial no. {cov['serial_no']}",
                        "invoice", "photos / video of the machine", "Dhadkan sound report") if x]  # fmt: skip
    out = head + [f"{L[0]}:"] + [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    out.append(f"{L[1]}: " + ", ".join(x for x in (c.get("name"), c.get("phone"), c.get("email")) if x))
    out.append(f"{L[2]}: " + "; ".join(keep))
    out += ["", f"{L[3]}:", claim_letter(cov, machine, fault_name, evidence, owner, factory)]
    return "\n".join(out)


def claim_letter(cov: dict[str, Any], machine: dict[str, Any], fault_name: str | None, evidence: str = "",
                 owner: str | None = None, factory: str | None = None) -> str:  # fmt: skip
    """A short claim intimation the owner can forward to the company as it is. English: what companies read."""
    b = cov.get("best") or {}
    ref = {"warranty": "Warranty card", "amc": "AMC contract", "insurance": "Policy"}.get(
        b.get("kind", ""), "Reference"
    )
    lines = [
        f"To: {b.get('provider') or 'the service department'}",
        f"Subject: {'Warranty' if b.get('kind') != 'insurance' else 'Claim'} intimation, {machine['name']}"
        + (f", serial no. {cov['serial_no']}" if cov.get("serial_no") else ""),
        "",
        f"{ref} no.: {b.get('number') or '-'}"
        + (f" (valid till {_date(b.get('end'), 'en')})" if b.get("end") else ""),
        f"Machine: {machine['name']} ({machine.get('type')}, {machine.get('motor_hp', '-')} HP motor)",
        f"Problem noticed: {fault_name or 'abnormal sound and vibration'}, on {_date(cov.get('on'), 'en')}.",
    ]
    if evidence:
        lines.append(f"Evidence: {evidence}")
    lines += [
        "The machine has not been opened or repaired by anyone yet.",
        "Please register the claim and send your engineer for inspection.",
        "",
        f"{owner or 'Owner'}" + (f", {factory}" if factory else ""),
    ]
    return "\n".join(lines)


def public(doc: dict[str, Any]) -> dict[str, Any]:
    """A document for the API: everything but the full text."""
    return {k: v for k, v in doc.items() if k != "text"} | {"has_text": bool(doc.get("text"))}


def for_agent(
    store: DocStore, mid: str, machine: dict[str, Any], max_chars: int = 6000
) -> list[dict[str, Any]]:
    """The papers as the agent reads them: the fields and the document text (so it can answer any question)."""
    store.seed(mid, machine)
    return [{"id": d["id"], "kind": d["kind"], "filename": d["filename"], "read_by": d["read_by"],
             "fields": {k: v for k, v in d["fields"].items() if k != "quotes"},
             "text": (d.get("text") or "")[:max_chars]} for d in store.list(mid)]  # fmt: skip
