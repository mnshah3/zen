"""Headline-to-company matching (zen.data.names.find_in_text), as the brief's bridge uses it.

The two false matches in the 10 Sep 2026 sample brief are pinned here, with the true
matches each rule must keep."""

from zen.data.names import find_in_text, phrase

NAMES = {
    "GOACARBON": "Goa Carbon Limited",
    "MGEL": "Mangalam Global Enterprise Limited",
    "RAIN": "Rain Industries Limited",
    "HIKAL": "Hikal Limited",
    "RIRPOWER": "RIR Power Electronics Limited",
    "ANDHRAPAP": "Andhra Paper Limited",
}
SYMS = list(NAMES)


def hits(text):
    return find_in_text(text, NAMES, SYMS)


def test_phrase_drops_the_legal_suffix():
    assert phrase("Goa Carbon Limited") == "goa carbon"
    assert phrase("Hikal Ltd.") == "hikal"


def test_a_lone_ordinary_word_is_not_a_company():
    assert "GOACARBON" not in hits("UK recognises India's CCTS; carbon price to be accepted under CBAM regime")
    assert "RAIN" not in hits("Heavy rain lashes Mumbai, local trains delayed")


def test_two_filler_words_do_not_corroborate_each_other():
    text = ("Fifty districts drive a third of the informal economy; a global survey of every "
            "enterprise finds GVA per worker above the all-India average")
    assert "MGEL" not in hits(text)


def test_the_whole_name_still_matches():
    assert hits("Goa Carbon shares hit upper circuit") == ["GOACARBON"]
    assert hits("Rain Industries posts a wider loss") == ["RAIN"]
    assert hits("Mangalam Global to raise funds") == ["MGEL"]


def test_existing_rules_unchanged():
    assert hits("Hikal wins a supply contract") == ["HIKAL"]
    assert "RIRPOWER" not in hits("Jaiprakash Power Ventures shares jump")
    assert hits("Andhra Paper raises prices") == ["ANDHRAPAP"]
