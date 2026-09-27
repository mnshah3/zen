"""README.md's figures against the files they come from.

Every block of README.md between `<!-- numbers:NAME:start -->` and
`<!-- numbers:NAME:end -->` must equal what jobs/readme_numbers.py renders
from the committed results (data/backtest/v1_final and the runs it is compared
with) and, when the database is there, from data/zen.duckdb counted up to
ARCHIVE_AS_OF. Without the database the archive blocks are skipped, not
passed: the skip names them. Fix a failure with
`python -m jobs.readme_numbers --write` after checking why the source moved.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from jobs import readme_numbers as rn

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def rendered():
    return rn.render(ROOT)


@pytest.fixture(scope="module")
def readme_blocks():
    return rn.read_blocks((ROOT / "README.md").read_text(encoding="utf-8"))


def test_every_block_has_a_generator_and_every_generator_a_block(rendered, readme_blocks):
    blocks, skipped = rendered
    assert set(readme_blocks) == set(blocks) | set(skipped)
    assert set(skipped) <= set(rn.ARCHIVE_BLOCKS)


def test_every_marked_block_equals_what_the_generator_renders(rendered, readme_blocks):
    blocks, skipped = rendered
    stale = [name for name, text in blocks.items() if readme_blocks[name] != text]
    assert not stale, f"README.md is out of date in {stale}: run python -m jobs.readme_numbers --write"
    if skipped:
        pytest.skip("archive blocks not checked: " + "; ".join(sorted(set(skipped.values()))))


def test_without_the_database_only_the_archive_blocks_are_skipped(rendered, tmp_path):
    blocks, _ = rendered
    alone, skipped = rn.render(ROOT, db=tmp_path / "absent.duckdb")
    assert set(skipped) == set(rn.ARCHIVE_BLOCKS)
    assert alone == {k: v for k, v in blocks.items() if k not in rn.ARCHIVE_BLOCKS}


def test_writing_an_up_to_date_readme_changes_nothing(rendered):
    blocks, _ = rendered
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    fresh = rn.replace_blocks(text, blocks)
    assert rn.read_blocks(fresh) == rn.read_blocks(rn.replace_blocks(fresh, blocks))


def test_malformed_markers_are_refused():
    ok = "a\n<!-- numbers:x:start -->\nb\n<!-- numbers:x:end -->\nc\n"
    assert rn.read_blocks(ok) == {"x": "b"}
    for bad in ("<!-- numbers:x:start -->\nb\n",                                   # no end
                "<!-- numbers:x:start -->\n<!-- numbers:y:start -->\n",             # nested
                "<!-- numbers:x:end -->\n",                                        # end only
                ok + ok,                                                           # twice
                "<!-- numbers:X:start -->\n<!-- numbers:X:end -->\n"):             # bad name
        with pytest.raises(ValueError):
            rn.read_blocks(bad)


def test_a_figure_ending_in_five_is_not_rounded_again():
    assert rn.one_dp(34.31) == "34.3"
    assert rn.one_dp(20.96) == "21.0"
    with pytest.raises(ValueError):
        rn.one_dp(12.75)


def test_wrapping_never_starts_a_line_markdown_would_misread():
    for token in ("- dash", "1. one", "# hash", "> quote", "| pipe", "+ plus"):
        text = "abcd " * 6 + token + " " + "abcd " * 6        # a plain wrap at 30 starts line 2 with it
        assert f"\n{token}" in "\n" + "\n".join(textwrap.wrap(text, 30))
        lines = rn.wrap(text, width=30).splitlines()
        assert " ".join(lines).split() == text.split()
        assert not any(line.startswith(("- ", "1. ", "#", ">", "|", "+ ")) for line in lines[1:])
