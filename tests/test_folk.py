from dhadkan import folk


def test_word_start_only():
    lex = folk.load_lexicon()
    assert folk.extract("chatak se toot gaya", lex) == []      # "atak" inside a word


def test_two_sounds_split_weight():
    lex = folk.load_lexicon()
    r = folk.label_text("pehle seeti, ab khat khat", lex)
    assert r["sound"] == "seeti+khat-khat"
    assert abs(sum(r["scores"].values()) - 1.0) < 1e-9


def test_miss_without_llm_is_unknown():
    r = folk.label_text("कुछ अजीब सा है", folk.load_lexicon(), llm=None)
    assert r["fault"] == "unknown" and r["source"] == "none"


def test_lexicon_weights_sum_to_one():
    for s in folk.load_lexicon()["sounds"]:
        assert abs(sum(s["faults"].values()) - 1.0) < 1e-9, s["sound"]


def test_asr_spelling_variants_fold():
    lex = folk.load_lexicon()
    # Whisper output for a synthetic Hindi note: dental त and a halant instead of खटखट
    assert folk.label_text("ब्लूर में खत्खत की आबाज आरही है", lex)["sound"] == "khat-khat"
