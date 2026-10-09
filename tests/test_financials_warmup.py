"""The NSE warm-up before a financials listing (zen/data/financials.py `_warm`)."""

from __future__ import annotations

import pytest

from zen.data import financials as fin


class Flaky:
    def __init__(self, fails: int):
        self.fails, self.calls = fails, 0

    def get(self, url, timeout):
        self.calls += 1
        if self.calls <= self.fails:
            raise TimeoutError("read timed out")


def test_a_slow_home_page_is_retried(monkeypatch):
    monkeypatch.setattr(fin.time, "sleep", lambda s: None)
    s = Flaky(fails=1)
    fin._warm(s)
    assert s.calls == 2


def test_a_page_that_never_answers_is_passed_over(monkeypatch):
    monkeypatch.setattr(fin.time, "sleep", lambda s: None)
    s = Flaky(fails=99)
    fin._warm(s)                         # the listing goes on with the home page's cookies
    assert s.calls == 2


def test_the_listing_itself_still_refuses_to_come_back_short(monkeypatch):
    monkeypatch.setattr(fin.time, "sleep", lambda s: None)

    class Down:
        def get(self, url, timeout):
            raise TimeoutError("read timed out")
    with pytest.raises(RuntimeError, match="refusing to return a partial window"):
        fin.listing(start=fin.date(2026, 10, 2), end=fin.date(2026, 10, 9), session=Down())
