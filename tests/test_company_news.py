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
    for t in ["Hindalco Industries Share Price", "Aurobindo Pharma (NSEI:AUROPHARMA) Stock Price",
              "Bajaj Auto Share Price Prediction 2027: Target Rs 11,399-12,757",
              "Hero MotoCorp Xtreme 250R Grille Image \u2013 Xtreme 250R Photos in India",
              "Bajaj Auto (BAJAJ-AUTO) Share Price NSE",
              "HERO MOTOCORP LIMITED Option Chain - Live HERO MOTOCORP LIMITED Option Chain Data, OI & Price"]:
        assert cn.NOT_NEWS.search(t), t
    for t in ["Aurobindo Pharma recalls over 1.1 lakh bottles of paracetamol in US",
              "Uflex, Spectra Systems Bid for RBI Polymer Banknote Tender",
              "Why did the Hero MotoCorp share price fall today after the RBI move?"]:
        assert not cn.NOT_NEWS.search(t), t


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Bajaj Auto stock trades at INR 9,820.00 - AD HOC NEWS</title><link>https://a.example/1</link>
<source url="https://a.example">AD HOC NEWS</source><pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Bajaj Auto Ltd. is Rated Hold - MarketsMojo</title><link>https://b.example/2</link>
<source url="https://b.example">MarketsMojo</source><pubDate>Thu, 08 Oct 2026 09:00:00 GMT</pubDate></item>
<item><title>Bajaj Auto September sales rise 5% - Business Standard</title><link>https://c.example/3</link>
<source url="https://c.example">Business Standard</source><pubDate>Wed, 07 Oct 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""


def _feed(monkeypatch, items):
    body = "".join(f'<item><title>{t} - {s}</title><link>https://x.example/{i}</link><source url="https://x.example">{s}</source>'
                   f"<pubDate>Wed, 07 Oct 2026 0{i % 10}:00:00 GMT</pubDate></item>" for i, (t, s) in enumerate(items))
    xml = f'<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'.encode()

    class R:
        content = xml

        def raise_for_status(self):
            pass
    monkeypatch.setattr(cn.requests, "get", lambda *a, **k: R())


def test_galleries_web_address_outlets_and_tickers_are_dropped(monkeypatch):
    _feed(monkeypatch, [
        ("Hero MotoCorp Xpulse 210 Grille", "Autocar India"),
        ("Hero MotoCorp Xpulse 210 Side Stand", "Autocar India"),
        ("Hero MotoCorp Xpulse 210 Wheel", "Autocar India"),
        ("HEROMOTOCO.NS Q2 2026 Earnings: 22% Revenue Growth", "Some Site"),
        ("Hero MotoCorp Q2 Earnings beat estimates", "https://www.example.in/"),
        ("Hero MotoCorp Share Price News: fundamentals and sector view", "Univest"),
        ("Hero MotoCorp launches the Xpulse 210 at Rs 1.6 lakh", "Autocar India"),
        ("Hero MotoCorp September sales rise 8%", "Business Standard"),
        ("Hero MotoCorp Xtreme 160R 4V Rear Left Three Quarter", "Autocar India"),
        ("Hero MotoCorp Xpulse 210 Front Wheel", "Autocar India"),
        ("Hero MotoCorp Xoom 125 Color Matt Giallo Lime", "Autocar India"),
        ("Hero MotoCorp Splendor+ Xtec 2.0 Color Matte Grey", "Autocar India"),
    ])
    got = [x["t"] for x in cn.headlines("Hero MotoCorp Limited", "HEROMOTOCO")]
    assert sorted(got) == ["Hero MotoCorp September sales rise 8%", "Hero MotoCorp launches the Xpulse 210 at Rs 1.6 lakh"]


def test_a_vehicle_site_keeps_its_news_and_loses_its_catalogue():
    assert cn._catalogue_entry("Hero MotoCorp Xtreme 125R Color Abrax Orange", "Autocar India")
    assert not cn._catalogue_entry("Hero MotoCorp sales rise 8% in September", "Autocar India")
    assert not cn._catalogue_entry("Bhansali Engineering Polymers Commissions Solar Plant", "EquityBulls")


def test_one_story_from_many_outlets_is_shown_once(monkeypatch):
    _feed(monkeypatch, [
        ("Bhansali Engineering Polymers Commissions 1,300 KW Solar Power Plant at Satnoor Facility", "EquityBulls"),
        ("Bhansali Engineering Polymers Commissions 1300 KW Dc Solar Plant With 64.2 Million Rupees Capex", "TradingView"),
        ("Bhansali Engineering Polymers Sets Up 1,300 KW Solar Plant in Madhya Pradesh", "Sahi"),
        ("Bhansali Engineering commissions 1,300 KW solar plant at Satnoor unit", "scanx.trade"),
        ("Bhansali Engineering Polymers Spends Rs 9.42 Crore on CSR in FY 2025-26", "India CSR"),
    ])
    got = cn.headlines("Bhansali Engineering Polymers Limited", "BEPL")
    assert len(got) == 2
    assert sum("Solar" in x["t"] or "solar" in x["t"] for x in got) == 1


def test_different_stories_that_share_a_few_words_stay_apart():
    core = "Bajaj Auto"
    a = cn._words("Auto sector stocks surge today, October 9: Eicher Motors jumps 3.37%, Bajaj Auto up 2.09%", core)
    b = cn._words("Auto stocks extend fall after RBI rate hike: TVS Motor, Bajaj Auto, Hero MotoCorp among top losers", core)
    assert not cn.same_story(a, b)
    hd = "HDFC Bank"
    assert not cn.same_story(cn._words("HDFC Bank rallies Friday, outperforms market", hd),
                             cn._words("HDFC Bank shares bounce back after rate hike blow; YTD loss tops 29%", hd))


def test_two_stories_from_one_outlet_are_not_a_gallery(monkeypatch):
    _feed(monkeypatch, [
        ("HDFC Bank Q2 results: net profit rises 11% to Rs 19,500 crore", "Moneycontrol.com"),
        ("HDFC Bank Q2 results: deposits grow faster than loans for a third quarter", "Moneycontrol.com"),
    ])
    assert len(cn.headlines("HDFC Bank Limited", "HDFCBANK")) == 2


def test_machine_written_outlets_are_dropped(monkeypatch):
    class R:
        content = RSS

        def raise_for_status(self):
            pass
    monkeypatch.setattr(cn.requests, "get", lambda *a, **k: R())
    got = cn.headlines("Bajaj Auto Limited", "BAJAJ-AUTO")
    assert [x["t"] for x in got] == ["Bajaj Auto September sales rise 5%"]
    assert got[0]["src"] == "Business Standard"


def test_a_network_failure_is_an_empty_list(monkeypatch):
    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(cn.requests, "get", boom)
    assert cn.headlines("Oil India Limited", "OIL") == []
