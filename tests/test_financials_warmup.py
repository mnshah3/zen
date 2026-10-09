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
    s = Flaky(fails=2)
    fin._warm(s)
    assert s.calls == 3


def test_a_page_that_never_answers_still_fails_the_run(monkeypatch):
    monkeypatch.setattr(fin.time, "sleep", lambda s: None)
    s = Flaky(fails=99)
    with pytest.raises(TimeoutError):
        fin._warm(s)
    assert s.calls == 3
