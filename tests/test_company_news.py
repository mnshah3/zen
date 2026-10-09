"""Headline matching for company news (zen/data/company_news.py)."""

from __future__ import annotations

import pytest

from zen.data import company_news as cn

CASES = [
    ("Larsen & Toubro wins a large order", "Larsen & Toubro Limited", "LT", True),
    ("TCS Q2 results: profit rises", "Tata Consultancy Services Limited", "TCS", True),
    ("Tata Motors shares fall", "Tata Consultancy Services Limited", "TCS", False),
    ("Moscow's Cheap Oil: India's Strategic Tightrope", "Oil India Limited", "OIL", False),
    ("Sensex rebounds as IT stocks rally and oil eases", "Oil India Limited", "OIL", False),
    ("Oil India vs ONGC: valuation premium", "Oil India Limited", "OIL", True),
    ("Bhansali Engineering commissions a solar plant", "Bhansali Engineering Polymers Limited", "BEPL", True),
    ("Great Eastern Shipping (NSE:GESHIP) certificate", "The Great Eastern Shipping Company Limited", "GESHIP", True),
]


@pytest.mark.parametrize("title,name,symbol,want", CASES)
def test_a_headline_must_name_the_company(title, name, symbol, want):
    assert cn.mentions(title, name, symbol) is want


def test_core_name_and_quote_pages():
    assert cn.core_name("Aurobindo Pharma Limited") == "Aurobindo Pharma"
    assert cn.core_name("Akme Fintrade (India) Limited") == "Akme Fintrade"
    assert cn.NOT_NEWS.search("Aurobindo Pharma Limited (AUROPHARMA.NS) stock price, news, quote and history")


def test_a_network_failure_is_an_empty_list(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(cn.requests, "get", boom)
    assert cn.headlines("Oil India Limited", "OIL") == []
