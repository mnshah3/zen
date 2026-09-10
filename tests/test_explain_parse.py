"""Parsing the model's answer, including when it stops halfway.

The brief asks for twenty-odd explanations in one response, roughly eight
kilobytes of JSON. A free-tier model truncates that often enough to matter, and
the original parser needed a complete array: one missing closing bracket threw
away every complete object that had arrived. On a live send that turned a
model which had answered into "0 of 23 stories explained".

So the case worth testing is not the happy one.
"""

from __future__ import annotations

from zen.monitor.explain import _parse

GOOD = '[{"i": 1, "s": "Crude rose, which widens India\'s trade deficit."}, ' \
       '{"i": 2, "s": "The ECB raised rates, pressuring the rupee."}]'


def test_clean_array():
    out = _parse(GOOD, expected=2)
    assert len(out) == 2
    assert out[1].startswith("Crude rose")


def test_markdown_fence():
    out = _parse(f"```json\n{GOOD}\n```", expected=2)
    assert len(out) == 2


def test_prose_before_the_array():
    out = _parse(f"Here are the explanations you asked for:\n\n{GOOD}", expected=2)
    assert len(out) == 2


def test_truncated_array_is_salvaged():
    """The failure that produced an unexplained brief."""
    truncated = ('[{"i": 1, "s": "Crude rose, which widens the trade deficit."}, '
                 '{"i": 2, "s": "The ECB raised rates, pressuring the rupee."}, '
                 '{"i": 3, "s": "Sugar prices hit a 17-month hi')
    out = _parse(truncated, expected=3)
    assert len(out) == 2, "should recover the two complete objects"
    assert 3 not in out, "the incomplete object must not be half-recovered"


def test_braces_and_quotes_inside_an_explanation():
    """Why the salvage decodes rather than pattern-matches."""
    tricky = ('[{"i": 1, "s": "The RBI said \\"no change\\" to rates {for now}."}, '
              '{"i": 2, "s": "Oil rose 5%.')
    out = _parse(tricky, expected=2)
    assert out[1] == 'The RBI said "no change" to rates {for now}.'


def test_indices_outside_the_range_are_ignored():
    out = _parse('[{"i": 0, "s": "zero"}, {"i": 99, "s": "way out"}, '
                 '{"i": 1, "s": "fine"}]', expected=2)
    assert out == {1: "fine"}


def test_garbage_returns_empty_rather_than_raising():
    assert _parse("I could not complete that request.", expected=5) == {}
    assert _parse("", expected=5) == {}
