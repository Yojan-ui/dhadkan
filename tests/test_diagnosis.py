"""faults.yaml schema, the severity mapping, diagnosis cards and the alert text."""

import copy

import pytest

from server import diagnosis as d


@pytest.fixture()
def kb():
    return copy.deepcopy(d.load())


def test_knowledge_base_is_complete():
    kb = d.load()
    assert set(kb["faults"]) == {
        "bearing_wear", "misalignment", "imbalance", "belt_slip", "cavitation", "motor_winding", "looseness"
    }  # fmt: skip
    for f in kb["faults"].values():
        assert f["repairable"]["answer"] in (True, False) and f["repairable"]["why"]
        assert f["misuse"], "every fault lists how it gets damaged"
        assert f["fix"]["technician"]["en"]


@pytest.mark.parametrize(
    "breakage, message",
    [
        (lambda kb: kb["faults"]["belt_slip"]["fix"].update(price_source=None), "needs a source"),
        (lambda kb: kb["faults"]["imbalance"]["fix"].update(price_source="made_up"), "unknown source"),
        (lambda kb: kb["faults"]["looseness"]["what"].pop("mr"), "en/hi/mr"),
        (lambda kb: kb["faults"]["cavitation"].pop("warranty"), "missing"),
        (
            lambda kb: kb["faults"]["imbalance"]["warranty"].update(period="2 years", source=None),
            "warranty period",
        ),
        (lambda kb: kb["faults"]["belt_slip"]["fix"].update(price_inr=[800, 100]), "low, high"),
        (lambda kb: kb["naming"]["owner_word"].update(bearing="gremlins"), "unknown fault"),
        (lambda kb: kb["sources"]["moglix_6205"].pop("url"), "missing a field"),
    ],
)
def test_schema_rejects_broken_entries(kb, breakage, message):
    breakage(kb)
    with pytest.raises(d.KnowledgeBaseError, match=message):
        d.validate(kb)


def test_every_price_and_warranty_period_is_sourced():
    kb = d.load()
    for key, f in kb["faults"].items():
        fix = f["fix"]
        if fix.get("price_inr") or fix.get("price_inr_per_hp"):
            assert fix["price_source"] in kb["sources"], key
        if f["warranty"].get("period"):
            assert f["warranty"]["source"] in kb["sources"], key


EST = lambda status, low=None: {"status": status, "low": low}  # noqa: E731


@pytest.mark.parametrize(
    "status, est, days_at_fault, expected",
    [
        ("ok", EST("no_trend"), 0, None),
        ("ok", None, 0, None),
        ("watch", EST("no_trend"), 0, "watch"),
        ("ok", EST("estimate", 20), 0, "watch"),  # a slow trend is worth watching
        ("ok", EST("estimate", 10), 0, "repair_soon"),
        ("alarm", EST("no_trend"), 0, "repair_soon"),
        ("early_match", EST("too_few"), 0, "repair_soon"),
        ("alarm", EST("estimate", 1.5), 0, "stop_now"),
        ("alarm", EST("at_fault_level"), 1, "stop_now"),
        ("alarm", EST("at_fault_level"), 4, "replace"),
        ("ok", EST("not_separable"), 0, None),
    ],
)
def test_severity_mapping(status, est, days_at_fault, expected):
    assert d.severity(status, est, days_at_fault) == expected


def test_severity_is_monotonic_in_days():
    levels = [d.severity("ok", EST("estimate", days)) for days in (30, 14, 10, 2, 0.5)]
    order = [None, *d.SEVERITIES]
    assert [order.index(x) for x in levels] == sorted(order.index(x) for x in levels)


def test_fault_naming_prefers_the_classifier():
    assert d.fault_for(owner_word="loose_belt") == "belt_slip"
    assert d.fault_for(owner_word="bearing", classifier="outer_race") == "bearing_wear"
    assert d.fault_for(owner_word="clog") == "cavitation"
    assert d.fault_for() is None


MACHINE = {
    "id": "A-G1",
    "type": "gearbox",
    "status": "alarm",
    "known_fault": "bearing",
    "forecast": {"status": "estimate", "low": 6, "high": 12, "crossings": [4, 6, 8, 9, 12, 30]},
}


def test_card_money_comes_from_the_knowledge_base_and_profile():
    c = d.card(MACHINE, "A", "bearing_wear", hp=2, service_h=1.5)
    kb, prof = d.load(), d.costs.load()
    part = kb["faults"]["bearing_wear"]["fix"]
    rate = prof["technician_rate_inr_per_hour"]
    assert c["fix"]["total_inr"] == [part["price_inr"][0] + part["labour_hours"][0] * rate,
                                     part["price_inr"][1] + part["labour_hours"][1] * rate]  # fmt: skip
    assert c["if_ignored"]["repair_inr"] == 2 * kb["faults"]["motor_winding"]["fix"]["price_inr_per_hp"]
    assert c["money"]["p_fault_within_wait"] == pytest.approx(
        2 / 6, abs=0.01
    )  # crossings 4 and 6 are within 7 days
    assert c["severity"]["level"] == "repair_soon" and c["days"] == 6


@pytest.mark.parametrize(
    "lang, reply", [("hi", "1 = मिस्त्री बुलाओ"), ("mr", "1 = मिस्त्री बोलवा"), ("en", "1 = send mechanic")]
)
def test_message_template(lang, reply):
    c = d.card(MACHINE, "A", "bearing_wear", hp=2, service_h=1.5, lang=lang)
    text = d.message(c, "Loom 1", days_rising=3, lang=lang)
    assert text.startswith("⚠ ") and reply in text
    assert d.load()["disclaimer"][lang] in text
    lo, hi = c["fix"]["total_inr"]
    assert f"₹{lo:,}-{hi:,}" in text
    assert "₹2,000" in text  # 2 HP x Rs 1,000/HP rewind, the cost of letting it burn
    assert "6" in text


def test_unnamed_fault_still_gets_a_card():
    c = d.card(MACHINE, "A", None, hp=2, service_h=1.5)
    assert c["fault"] is None and "mechanic" in c["unknown"]["en"]
    assert "3 = " in d.message(c, "Loom 1", 0, "en")


def test_healthy_machine_gets_no_card():
    assert (
        d.card({**MACHINE, "status": "ok", "forecast": {"status": "no_trend"}}, "A", "bearing_wear", 2, 1.5)
        is None
    )


def test_roman_hindi_alert_matches_the_brief_style():
    m = {"id": "A-G1", "type": "gearbox", "status": "alarm", "days_at_fault": 0,
         "forecast": {"status": "estimate", "low": 3, "mid": 5, "high": 9, "crossings": [3, 5, 9]}}  # fmt: skip
    c = d.card(m, "A", "bearing_wear", 2, 1.5, "hi_latn")
    text = d.message(c, "Loom 1", 3, "hi_latn")
    assert (
        text.startswith("⚠ ") and "bearing ghis raha hai" in text and "3 din se aawaz badh rahi hai" in text
    )
    assert "Reply 1 = mistri bulao, 2 = baad mein, 3 = poori jaankari" in text
    assert all(ord(ch) < 0x900 or ord(ch) > 0x97F for ch in text)  # no Devanagari
