"""zen.data.industry.fetch_symbol: a stock NSE knows only under BE is still labelled."""
from zen.data import industry


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class Client:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def get(self, symbol, series):
        self.asked.append(series)
        return self.answers[series]


def payload():
    return {"equityResponse": [{"secInfo": {"isin": "INE000X01010", "macro": "Industrials", "sector": "Capital Goods",
                                            "basicIndustry": "Ship Building & Allied Services", "industryInfo": "Aerospace & Defense"},
                                "metaData": {"series": "BE", "companyName": "Swan Defence"}}]}


def test_a_404_under_eq_still_asks_be():
    c = Client({"EQ": Resp(404), "BE": Resp(200, payload())})
    industry.fetch_symbol(c, "SWANDEF")
    assert c.asked == ["EQ", "BE"]


def test_rights_entitlements_are_not_asked_for(tmp_path, monkeypatch):
    import pandas as pd
    from jobs import fetch_industry
    d = tmp_path / "data" / "daily" / "2026"
    d.mkdir(parents=True)
    pd.DataFrame({"date": pd.to_datetime(["2026-10-28"] * 4), "symbol": ["AAA", "BBB", "OLAELEC-RE", "JAYKAY-RE1"],
                  "series": ["EQ", "BE", "BE", "BE"]}).to_parquet(d / "2026-10.parquet")
    monkeypatch.chdir(tmp_path)
    eq, lo, hi = fetch_industry.recent_eq_symbols(sessions=1)
    assert eq == {"AAA", "BBB"}
