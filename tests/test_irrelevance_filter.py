"""The irrelevance filter, and the renumbering it forces.

Two things can go wrong here and only one of them is obvious.

The obvious one: the pattern fails to match a dismissal and the brief keeps
carrying stories the explainer already called irrelevant. That happened twice
while writing it -- a shell heredoc turned every \\b into a literal backspace
byte, so the regex compiled cleanly and matched nothing.

The dangerous one: the renderer keys explanations by POSITION in the story
list. Remove story 3 and every explanation after it shifts by one, so story 4
inherits story 3's text. Every remaining story would carry a plausible,
confidently-worded explanation of a different story. That is far worse than the
wasted slots the filter exists to remove, and it is invisible unless something
checks the pairing survives.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobs.daily_brief import IRRELEVANT


DISMISSALS = [
    "This Australian expansion has no direct impact on Indian markets.",
    "This US technology partnership has no direct impact on Indian markets.",
    "This European lawsuit has no direct impact on Indian markets.",
    "This US initial public offering has no direct impact on Indian markets or companies.",
    "This micro-cap stock movement is trivial and has no broader impact on the Indian market.",
    "This global industry round-up has no direct, immediate impact on Indian markets.",
]

REAL = [
    "Brent crude jumped 5% to cross $106. For India, which imports over 80% of "
    "its crude, this is bad news. It expands our trade deficit.",
    "Oracle's revenue beat estimates. Indian IT giants like TCS and Infosys earn "
    "significant revenue helping global clients manage Oracle systems.",
    "Premier Energies is building a 12 GWh battery storage plant in Telangana, "
    "reducing India's heavy reliance on Chinese imports.",
]


def test_dismissals_are_matched():
    for text in DISMISSALS:
        assert IRRELEVANT.search(text), f"missed a dismissal: {text}"


def test_real_explanations_survive():
    for text in REAL:
        assert not IRRELEVANT.search(text), f"dropped a real explanation: {text}"


def test_pattern_holds_no_control_characters():
    """Guards the specific bug that broke this twice."""
    assert "\x08" not in IRRELEVANT.pattern
    assert not any(ord(c) < 32 for c in IRRELEVANT.pattern)


@dataclass
class FakeArticle:
    title: str
    themes: list = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.title


def _renumber(sections, explanations, facts_map):
    """The renumbering exactly as daily_brief performs it."""
    ordered = [a for arts in sections.values() for a in arts]
    keyed = {a.key: i for i, a in enumerate(ordered, start=1)}
    dismissed = {a.key for a in ordered
                 if (t := explanations.get(keyed[a.key])) and len(t) < 320
                 and IRRELEVANT.search(t)}
    if not dismissed:
        return sections, explanations, facts_map

    sections = {name: kept for name, arts in sections.items()
                if (kept := [a for a in arts if a.key not in dismissed])}
    by_key = {a.key: (explanations.get(keyed[a.key]), facts_map.get(keyed[a.key]))
              for a in ordered}
    ordered = [a for arts in sections.values() for a in arts]
    explanations, facts_map = {}, {}
    for i, a in enumerate(ordered, start=1):
        exp, fac = by_key.get(a.key, (None, None))
        if exp:
            explanations[i] = exp
        if fac:
            facts_map[i] = fac
    return sections, explanations, facts_map


def test_survivors_keep_their_own_explanation():
    """The failure that matters: a story inheriting its neighbour's text."""
    sections = {
        "Breaking": [FakeArticle("keep-1"), FakeArticle("drop-1")],
        "Macro": [FakeArticle("drop-2"), FakeArticle("keep-2"), FakeArticle("keep-3")],
    }
    explanations = {1: "keep-1 explanation, about crude and the rupee",
                    2: DISMISSALS[0],
                    3: DISMISSALS[2],
                    4: "keep-2 explanation, about Oracle and Indian IT",
                    5: "keep-3 explanation, about battery storage"}
    facts_map = {1: ["1%"], 4: ["4%"], 5: ["5%"]}

    sections, explanations, facts_map = _renumber(sections, explanations, facts_map)
    ordered = [a for arts in sections.values() for a in arts]

    assert [a.key for a in ordered] == ["keep-1", "keep-2", "keep-3"]
    for i, a in enumerate(ordered, start=1):
        assert explanations[i].startswith(a.key), (
            f"{a.key} got: {explanations[i]!r}")
    assert facts_map == {1: ["1%"], 2: ["4%"], 3: ["5%"]}


def test_empty_section_is_removed_not_left_blank():
    sections = {"Breaking": [FakeArticle("a")], "Themes": [FakeArticle("b")]}
    explanations = {1: "a real explanation about Indian refiners", 2: DISMISSALS[1]}
    sections, _, _ = _renumber(sections, explanations, {})
    assert "Themes" not in sections
    assert [a.key for a in sections["Breaking"]] == ["a"]


def test_long_text_mentioning_no_impact_is_kept():
    """A real explanation may dismiss one leg before explaining another."""
    long = ("The Australian arm has no direct impact on Indian markets, but the "
            "same order book covers a Chennai facility that supplies three listed "
            "Indian component makers, and the capacity commitment there is what "
            "moves their revenue guidance for the next two financial years. "
            "Watch the margin line rather than the headline order value, since "
            "the Indian leg is contract manufacturing at thinner margins.")
    assert len(long) >= 320
    sections = {"Macro": [FakeArticle("x")]}
    _, exps, _ = _renumber(sections, {1: long}, {})
    assert exps.get(1) == long
