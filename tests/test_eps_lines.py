"""zen/data/financials.py, EPS ON ANOTHER LINE: a quarter's EPS filed on the wrong line is read from the
document's own other lines, and only then (the filings named are the ones the rule was written from)."""

from __future__ import annotations

from zen.data import financials as fin

HEAD = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:in-bse-fin="http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin">')
CTX = ('<xbrli:context id="OneD"><xbrli:entity><xbrli:identifier scheme="http://www.nseindia.com">X</xbrli:identifier>'
       '</xbrli:entity><xbrli:period><xbrli:startDate>2026-04-01</xbrli:startDate><xbrli:endDate>2026-06-30</xbrli:endDate>'
       '</xbrli:period></xbrli:context>')


def doc(**lines) -> bytes:
    names = {"profit": "ProfitLossForPeriod", "cont": "BasicEarningsLossPerShareFromContinuingOperations",
             "cont_d": "DilutedEarningsLossPerShareFromContinuingOperations",
             "disc": "BasicEarningsLossPerShareFromDiscontinuedOperations",
             "both": "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
             "both_d": "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
             "profit_disc": "ProfitLossFromDiscontinuedOperationsAfterTax", "owners": "ProfitOrLossAttributableToOwnersOfParent",
             "paid": "PaidUpValueOfEquityShareCapital", "face": "FaceValueOfEquityShareCapital"}
    body = "".join(f'<in-bse-fin:{names[k]} contextRef="OneD" unitRef="INR">{v}</in-bse-fin:{names[k]}>' for k, v in lines.items())
    return (HEAD + CTX + body + "</xbrli:xbrl>").encode()


def parse(content):
    d = {}
    f = fin.parse_xbrl(content, detail=d)
    return f, d.get("eps_fix")


def test_an_eps_on_the_both_operations_line_with_zero_on_the_continuing_one_is_the_eps():
    # Shaily Engineering, consolidated, Jun 2026: continuing 0, discontinued 10.45, both 10.45; no discontinued operations
    f, fix = parse(doc(profit=480100000, cont=0, cont_d=0, disc=10.45, both=10.45, both_d=10.41))
    assert f["eps_basic"] == 10.45 and f["eps_diluted"] == 10.41 and fix == "both_line"
    assert not any(k.startswith("_") for k in f)                               # the helper lines are dropped


def test_a_real_discontinued_profit_keeps_the_continuing_eps_as_filed():
    f, fix = parse(doc(profit=480100000, cont=0, both=10.45, profit_disc=470000000))
    assert f["eps_basic"] == 0 and fix is None


def test_every_eps_line_at_zero_beside_a_profit_is_no_eps_not_zero():
    # BPL's consolidated Sep 2023 filing: Rs 9.78 crore of profit, every EPS line 0.00; 4.9 crore shares of Rs 10
    f, fix = parse(doc(profit=97798000, cont=0, cont_d=0, both=0, paid=490000000, face=10))
    assert "eps_basic" not in f and fix == "not_given"
    tiny, fix2 = parse(doc(profit=100000, cont=0, both=0, paid=490000000, face=10))   # 0.2 paise a share: 0.00 is right
    assert tiny["eps_basic"] == 0 and fix2 is None
    no_count, fix3 = parse(doc(profit=97798000, cont=0, both=0))                    # no share count to decide by
    assert no_count["eps_basic"] == 0 and fix3 is None


def test_a_loss_filed_with_a_positive_eps_of_its_own_size_gets_its_sign():
    # Orissa Minerals, Jun 2024: a Rs 2.0781 crore loss, 60 lakh shares of Rs 1, EPS filed as 3.46
    f, fix = parse(doc(profit=-20781000, cont=3.46, cont_d=3.46, both=3.46, paid=6000000, face=1))
    assert f["eps_basic"] == -3.46 and f["eps_diluted"] == -3.46 and fix == "sign"
    owners, fix2 = parse(doc(profit=-20781000, owners=5000000, cont=0.83, paid=6000000, face=1))
    assert owners["eps_basic"] == 0.83 and fix2 is None                             # the owners' share is a profit: as filed
    far, fix3 = parse(doc(profit=-20781000, cont=1.20, paid=6000000, face=1))
    assert far["eps_basic"] == 1.20 and fix3 is None                                # not that profit over its shares: left


def test_a_both_line_eps_that_does_not_fit_the_profit_is_left_as_filed():
    # HLVL, Jun 2025: a Rs 3.47 crore loss, the both line +0.05; Gangotri Textiles, Jun 2025: -0.10 beside a
    # loss of Rs 2.38 lakh on 2.13 crore shares (that would be -0.01)
    f, fix = parse(doc(profit=-34700000, cont=0, both=0.05))
    assert f["eps_basic"] == 0 and fix is None
    g, fix2 = parse(doc(profit=-238000, cont=0, both=-0.10, paid=21345460, face=1))
    assert g["eps_basic"] == 0 and fix2 is None
    ok, fix3 = parse(doc(profit=-238000, cont=0, both=-0.01, paid=21345460, face=1))
    assert ok["eps_basic"] == -0.01 and fix3 == "both_line"
