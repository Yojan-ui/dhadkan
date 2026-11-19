"""The knowledge base (server/kb.py): content rules, retrieval, the index, and the planner's use of it. No network."""

import json
import re
import shutil

import numpy as np
import pytest
import yaml

from edge.config import MACHINE_TYPES
from server import kb, provider
from server.config import FAULTS
from server.planner import grounding
from tests.test_planner_core import Script, alarm, make_world, text, tool, turn

FAULT_KEYS = set(yaml.safe_load(FAULTS.read_text(encoding="utf-8"))["faults"])
RUPEE = re.compile(r"₹|\bRs\.?\s*\d|\brupee", re.IGNORECASE)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Ollama is down unless a test says otherwise: search is by keywords."""
    monkeypatch.setattr(provider, "available", lambda model=None: False)


# ---------------------------------------------------------------- content rules
def test_every_guide_has_front_matter_with_known_faults_and_machines():
    guides = [p for p in kb.KB_DIR.glob("*.md") if p.name != "README.md"]
    assert len(guides) >= 6
    for p in guides:
        meta, _ = kb._front(p.read_text(encoding="utf-8"))
        assert meta.get("title"), p.name
        assert set(meta["faults"]) <= FAULT_KEYS, p.name
        assert set(meta["machines"]) <= {*MACHINE_TYPES, "any"}, p.name
    covered = {f for p in guides for f in kb._front(p.read_text(encoding="utf-8"))[0]["faults"]}
    assert covered == FAULT_KEYS  # every fault card has a guide


def test_guides_hold_no_rupee_amount_and_no_part_number():
    """₹ and part numbers live only in faults.yaml (and tool outputs): one place to correct them."""
    for c in kb.guide_chunks():
        assert not RUPEE.search(c["text"]), c["id"]
        assert grounding.parts(c["text"]) == [], (c["id"], grounding.parts(c["text"]))


def test_guides_never_let_software_stop_a_machine():
    body = " ".join(c["text"].lower() for c in kb.guide_chunks())
    assert "no software here can stop" in body and "switched off" in body
    assert not re.search(r"\b(dhadkan|the agent|the system|software) (will|should|can) stop\b", body)


def test_fault_cards_carry_exactly_the_faults_yaml_figures():
    raw = yaml.safe_load(FAULTS.read_text(encoding="utf-8"))["faults"]
    cards = {c["id"]: c for c in kb.fault_chunks()}
    assert set(cards) == {f"faults.yaml#{k}" for k in raw}
    for k, f in raw.items():
        t, fix = cards[f"faults.yaml#{k}"]["text"], f["fix"]
        assert "TYPICAL, NOT A QUOTE" in t
        figures = {int(n.replace(",", "")) for n in re.findall(r"₹([\d,]+)", t)}
        if fix.get("price_inr"):
            assert figures == set(fix["price_inr"]), k
        elif fix.get("price_inr_per_hp"):
            assert figures == {fix["price_inr_per_hp"]} and "per HP" in t, k
        else:
            assert figures == set() and "not published" in t, k  # no figure invented for looseness


# ---------------------------------------------------------------- retrieval
QUERIES = [  # owner-style questions -> the guide or card that answers them
    ("blower se khat-khat awaaz, bearing housing garam", "fan", "bearing_wear"),
    ("pump sounds like it is pumping gravel, pressure dropped", "pump", "cavitation"),
    ("belt squeals at start-up, machine runs slow", "fan", "belt_slip"),
    ("motor smells burnt and trips the starter", None, "motor_winding"),
    ("fan shakes once per turn after flour built up on the blades", "fan", "imbalance"),
    ("coupling rubber dust after the motor was refitted", "pump", "misalignment"),
    ("machine rattles, base bolts loose", "gearbox", "looseness"),
]


@pytest.mark.parametrize(("q", "mtype", "want"), QUERIES)
def test_keyword_search_finds_the_right_fault_offline(q, mtype, want):
    hits = kb.search(q, k=3, machine_type=mtype)
    assert hits and all(h["method"] == "keyword" for h in hits)
    assert want in hits[0]["faults"], [h["id"] for h in hits]


def test_fault_filter_puts_its_card_and_guide_first():
    hits = kb.search("what does it cost and who fixes it", k=4, fault="bearing_wear")
    assert all("bearing_wear" in h["faults"] for h in hits)  # another fault's chunk never outranks the filter
    assert "faults.yaml#bearing_wear" in {h["id"] for h in hits}


def test_for_case_leads_with_the_fault_card():
    hits = kb.for_case("fan", "imbalance", "awaaz badal gayi")
    assert hits[0]["id"] == "faults.yaml#imbalance" and len(hits) == 3


def fake_embed(texts, model=None):
    """Deterministic bag-of-words vectors: enough to test the index without a model."""
    out = []
    for t in texts:
        v = np.zeros(256, np.float32)
        for w in kb.tokens(t):
            v[hash(w) % 256] += 1
        out.append(v.tolist())
    return out


def test_vector_index_builds_caches_and_refuses_stale_vectors(tmp_path, monkeypatch):
    monkeypatch.setattr(provider, "available", lambda model=None: True)
    calls = []
    monkeypatch.setattr(provider, "embed", lambda t, m=None: calls.append(len(t)) or fake_embed(t))
    d = tmp_path / "kb"
    shutil.copytree(kb.KB_DIR, d)
    cs = kb.guide_chunks(d) + kb.fault_chunks()
    idx = tmp_path / "index.npz"
    first = kb.build(idx, cs)
    assert first["embedded"] == len(cs) and idx.exists()
    assert kb.build(idx, cs)["embedded"] == 0  # cached by text hash: nothing re-embedded
    hits = kb.search("pump gravel noise low pressure", index=idx, cs=cs)
    assert hits[0]["method"] == "hybrid" and "cavitation" in hits[0]["faults"]
    # a guide edited after the build: the vectors are stale, so search falls back to keywords
    p = d / "cavitation.md"
    p.write_text(p.read_text(encoding="utf-8").replace("gravel", "stones and gravel"), encoding="utf-8")
    cs2 = kb.guide_chunks(d) + kb.fault_chunks()
    assert kb.search("pump gravel", index=idx, cs=cs2)[0]["method"] == "keyword"
    assert kb.build(idx, cs2)["embedded"] == 1  # only the edited section


def test_embedding_failure_falls_back_to_keywords(tmp_path, monkeypatch):
    monkeypatch.setattr(provider, "available", lambda model=None: True)
    monkeypatch.setattr(provider, "embed", lambda t, m=None: fake_embed(t))
    idx = tmp_path / "index.npz"
    kb.build(idx)

    def down(t, m=None):
        raise ConnectionRefusedError

    monkeypatch.setattr(provider, "embed", down)
    assert kb.search("belt squeal", index=idx)[0]["method"] == "keyword"


# ---------------------------------------------------------------- the planner uses it
def test_kb_figures_are_grounded_only_when_retrieved():
    card = next(c for c in kb.fault_chunks() if c["id"] == "faults.yaml#bearing_wear")
    reply = "Bearing badalne ka andaazan kharcha ₹65 se ₹180 hai (typical, not a quote)."
    assert grounding.check(reply, [{"chunks": kb.brief([card])}])["ok"]
    assert not grounding.check(reply, [{"chunks": []}])["ok"]  # not retrieved: an invented price


def test_planner_gets_knowledge_logs_it_and_can_search(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch)
    client = Script(
        turn(
            tool(
                "search_knowledge",
                query="gearbox noise rising, what to check",
                fault=None,
                machine_type="gearbox",
            )
        ),
        turn(
            text(
                "Gearbox ki awaaz badh rahi hai; pehle haath se jaanch. [kb/machines_and_safety.md#gearboxes-and-looms]"
            )
        ),
    )
    p = w.make(client)
    out = p.handle(alarm(w))
    ctx = json.loads(client.calls[0]["messages"][0]["content"])
    ids = [k["id"] for k in ctx["knowledge"]]
    assert ids and all(set(k) == {"id", "title", "source", "text"} for k in ctx["knowledge"])
    logged = [r for r in w.store.audit() if r["event"] == "kb_retrieved"]
    assert logged and logged[0]["detail"]["chunks"] == ids
    call = out["calls"][0]
    assert call["tool"] == "search_knowledge" and call["result"]["chunks"]
    assert call["result"]["method"] == "keyword" and "faults.yaml" in call["result"]["source"]
    assert "search_knowledge" in {t["name"] for t in client.calls[0]["tools"]}
