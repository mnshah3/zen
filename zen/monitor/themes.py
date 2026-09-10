"""The investor's own theses, as keyword tests.

The brief was ranking on trust, corroboration and recency alone -- a decent
newspaper front page and a poor personal brief. Two readers with opposite
portfolios got the identical email.

These themes are the investor's, stated in his words: bottlenecks exist where
India depends on external suppliers. Nothing here is inferred from his
holdings or invented on his behalf; if a theme is not something he said, it is
not in this file.

STRONG vs GENERIC, and why the split exists

An earlier categoriser matched RIR Power to any headline containing "power",
which put grid tariff stories into a nuclear bucket. So each theme carries two
vocabularies:

  strong   unambiguous in an Indian market context. One hit is enough.
  generic  real signal but promiscuous -- "power", "water", "capacity". These
           need a second hit from the same theme before they count at all.

The cost of getting this wrong is not a bad trade, it is a brief that cries
wolf until it stops being read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    blurb: str            # why this is on the list, in one line
    strong: tuple[str, ...]
    generic: tuple[str, ...] = ()


THEMES: tuple[Theme, ...] = (
    Theme(
        "AI & data centres",
        "compute capacity India cannot yet build domestically",
        strong=("data centre", "data center", "hyperscaler", "gpu", "nvidia",
                "semiconductor fab", "chip fab", "colocation", "ai infrastructure",
                "server rack", "liquid cooling"),
        generic=("artificial intelligence", "cloud", "capacity", "chip"),
    ),
    Theme(
        "Nuclear",
        "state-reserved today, but the supply chain around it is not",
        strong=("nuclear power", "smr", "small modular reactor", "uranium",
                "atomic energy", "npcil", "reactor", "thorium"),
        generic=("nuclear", "fuel cycle"),
    ),
    Theme(
        "Green energy & solar",
        "module and cell manufacturing still leans on imports",
        strong=("solar cell", "solar module", "photovoltaic", "polysilicon",
                "wafer", "electrolyser", "green hydrogen", "wind turbine",
                "battery storage", "bess", "pli solar"),
        generic=("solar", "renewable", "green energy", "battery"),
    ),
    Theme(
        "Water",
        "treatment and pipeline capacity, chronically under-built",
        strong=("water treatment", "desalination", "effluent treatment",
                "jal jeevan", "irrigation project", "sewage treatment", "pipeline water"),
        generic=("water", "treatment"),
    ),
    Theme(
        "Infrastructure",
        "the order books that turn government capex into revenue",
        strong=("order inflow", "order book", "epc contract", "letter of award",
                "highway project", "nhai", "metro rail", "port expansion",
                "dedicated freight", "railway electrification", "capex cycle"),
        generic=("infrastructure", "construction", "capex", "tender"),
    ),
    Theme(
        "Defence",
        "indigenisation targets against a large import base",
        strong=("defence order", "defence ministry", "drdo", "hal ", "indigenisation",
                "defence export", "missile", "artillery", "submarine", "fighter jet",
                "emergency procurement"),
        generic=("defence", "defense", "military"),
    ),
    Theme(
        "Import substitution",
        "the general case: where India buys abroad and could build at home",
        strong=("import substitution", "production linked incentive", "pli scheme",
                "make in india", "anti-dumping", "import duty", "domestic manufacturing",
                "china plus one", "supply chain shift", "localisation"),
        generic=("import", "domestic", "tariff"),
    ),
)

# Word-boundary matching, so "chip" does not fire on "chipotle" and "smr" does
# not fire inside a longer token. Substring matching is what let a single
# stray word carry a whole theme in the earlier categoriser.
_WORD = {}


def _pattern(term: str) -> re.Pattern:
    if term not in _WORD:
        _WORD[term] = re.compile(rf"(?<![a-z0-9]){re.escape(term.strip())}(?![a-z0-9])", re.I)
    return _WORD[term]


def _hits(text: str, terms: tuple[str, ...]) -> list[str]:
    return [t for t in terms if _pattern(t).search(text)]


def match(text: str) -> list[tuple[str, float, list[str]]]:
    """Themes this text touches, as (theme, strength 0-1, matched terms).

    One strong term is enough. Generic terms need corroboration -- two of them,
    or one alongside a strong term -- because on their own they match a third
    of the business pages.
    """
    out = []
    for th in THEMES:
        strong = _hits(text, th.strong)
        generic = _hits(text, th.generic)
        if strong:
            strength = min(1.0, 0.6 + 0.2 * len(strong) + 0.1 * len(generic))
        elif len(generic) >= 2:
            strength = 0.45          # corroborated, but no unambiguous term
        else:
            continue                 # one generic word is not a theme
        out.append((th.name, round(strength, 2), (strong + generic)[:4]))
    return sorted(out, key=lambda x: -x[1])


def affinity(text: str) -> float:
    """Single 0-1 score for how much this story is about the investor's themes."""
    m = match(text)
    return m[0][1] if m else 0.0


def blurb(name: str) -> str:
    return next((t.blurb for t in THEMES if t.name == name), "")
