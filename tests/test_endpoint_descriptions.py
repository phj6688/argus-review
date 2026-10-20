from argus.api import main


def test_first_sentence_joins_wrapped_lines():
    doc = """Record the operator's ground-truth label on a finding (the only real
    signal), then return it. Second sentence."""
    assert main._first_sentence(doc) == (
        "Record the operator's ground-truth label on a finding (the only real signal), then return it."
    )


def test_first_sentence_stops_at_paragraph_break():
    assert main._first_sentence("Shallow health check\n\nMore detail here.") == "Shallow health check"


def test_first_sentence_stops_at_whitespace_only_blank_line():
    assert main._first_sentence("Shallow health check\n    \n    More detail.") == "Shallow health check"


def test_first_sentence_keeps_dotted_tokens():
    doc = "Argus vs CodeRabbit: numbers from data/cr_threads.jsonl, mode=fast."
    assert main._first_sentence(doc) == doc


def test_first_sentence_empty_doc():
    assert main._first_sentence("") == ""
    assert main._first_sentence("   ") == ""


def test_real_endpoints_have_no_mid_sentence_cut():
    for entry in main._real_endpoints():
        assert "\n" not in entry["description"]
