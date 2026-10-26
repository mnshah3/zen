"""NSDL's fortnightly FPI investment by sector (zen.data.fpi_sectors): both table layouts read by their headers."""

from datetime import date, datetime

import pandas as pd

from zen.data import fpi_sectors as f

# the layout from 30 Apr 2022 (three header rows), cut to two sectors
OLD = """<html><body><table>
<tr><td></td><td></td><td colspan="10">AUC as on April 15, 2022</td><td colspan="10">Net Investment April 01-15, 2022</td>
<td colspan="10">Net Investment April 16-30, 2022</td><td colspan="10">AUC as on April 30, 2022</td></tr>
<tr><td></td><td></td>""" + '<td colspan="5">IN INR Cr.</td><td colspan="5">IN USD Mn</td>' * 4 + """</tr>
<tr><td>Sr. No.</td><td>Sectors</td>""" + "<td>Equity</td><td>Debt</td><td>Debt VRR</td><td>Hybrid</td><td>Total</td>" * 8 + """</tr>
<tr><td>1</td><td>Capital Goods</td>""" + "".join(f"<td>{v}</td>" for v in
    ["1,00,304", 432, 0, 0, "1,00,736", "13,159", 57, 0, 0, "13,216", "-1,397", -2, 0, 0, "-1,399", -183, 0, 0, 0, -183,
     -688, -2, 0, 0, -690, -90, 0, 0, 0, -90, "97,186", 426, 0, 0, "97,612", "12,717", 55, 0, 0, "12,772"]) + """</tr>
<tr><td></td><td>Grand Total</td>""" + "".join(f"<td>{v}</td>" for v in range(1, 41)) + """</tr>
<tr><td colspan="42">Note: NSDL has decided to rely on the BSE classification.</td></tr>
</table></body></html>"""

# today's layout (a fourth header row naming the groups), FPI equity before the mutual funds' equity
NEW_HEAD = ["Equity", "Debt General Limit", "Debt VRR", "Debt-FAR", "Hybrid", "Equity", "Debt General Limit", "Hybrid",
            "Solution oriented", "Other", "AIF", "Total"]
NEW = """<table>
<tr><td></td><td></td><td colspan="24">AUC as on September 30, 2026</td><td colspan="24">Net Investment September 16-30, 2026</td>
<td colspan="24">Net Investment October 01-15, 2026</td><td colspan="24">AUC as on October 15, 2026</td></tr>
<tr><td></td><td></td>""" + '<td colspan="12">IN INR Cr.</td><td colspan="12">IN USD Mn</td>' * 4 + """</tr>
<tr><td></td><td></td>""" + ('<td>Equity</td><td colspan="3">Debt</td><td>Hybrid</td><td colspan="5">Mutual Funds</td>'
                            '<td>Alternative Investment Funds (AIFs)</td><td></td>') * 8 + """</tr>
<tr><td>Sr. No.</td><td>Sectors</td>""" + "".join(f"<td>{h}</td>" for h in NEW_HEAD * 8) + """</tr>
<tr><td>1</td><td>Automobile and Auto Components</td>""" + "".join(
    f"<td>{(b * 100 + c) * (-1 if b in (2, 3) else 1):,}</td>" for b in range(1, 9) for c in range(1, 13)) + """</tr>
</table>"""


def test_the_2022_layout():
    df = f.parse_report(OLD, datetime(2026, 10, 25, 23, 0), "u")
    assert df["sector"].tolist() == ["Capital Goods", "Total"]               # the note is not a sector
    r = df.iloc[0]
    assert (r["period_end"], r["period_start"]) == (date(2022, 4, 30), date(2022, 4, 16))
    assert (r["net_eq_cr"], r["net_total_cr"], r["net_eq_usd"], r["net_total_usd"]) == (-688, -690, -90, -90)
    assert (r["auc_eq_cr"], r["auc_total_cr"], r["auc_eq_usd"], r["auc_total_usd"]) == (97186, 97612, 12717, 12772)


def test_todays_layout_takes_the_fpis_own_equity():
    r = f.parse_report(NEW, datetime(2026, 10, 25, 23, 0), "u").iloc[0]
    # block 5 is this fortnight's net investment in rupees, block 6 in dollars, 7 and 8 the assets at its end
    assert (r["net_eq_cr"], r["net_total_cr"], r["net_eq_usd"], r["net_total_usd"]) == (501, 512, 601, 612)
    assert (r["auc_eq_cr"], r["auc_total_cr"], r["auc_eq_usd"], r["auc_total_usd"]) == (701, 712, 801, 812)
    assert r["period_end"] == date(2026, 10, 15) and r["period_start"] == date(2026, 10, 1)


def test_listing_and_a_fortnight_read_once(tmp_path):
    page = ('<select id="ddlfortnighly"><option value="~/StaticReports/Fortnightly_Sector_wise_FII_Investment_Data/'
            'FIIInvestSector_Oct152026.html">OCT 15, 2026</option><option value="~/StaticReports/x/FIIInvestSector_Feb292024.html">'
            'FEB 29, 2024</option><option value="~/x/notareport.html">x</option></select>')
    got = f.list_reports(page)
    assert [d for d, _ in got] == [date(2026, 10, 15), date(2024, 2, 29)]
    assert got[0][1] == f.BASE + "StaticReports/Fortnightly_Sector_wise_FII_Investment_Data/FIIInvestSector_Oct152026.html"
    first = f.parse_report(OLD, datetime(2026, 10, 25, 23, 0), "u")
    f.write(first, tmp_path)
    f.write(first.assign(observed_dt=datetime(2026, 11, 1), net_eq_cr=0.0), tmp_path)   # read again later: the first reading stays
    d = f.stored(tmp_path)
    assert len(d) == 2 and d["observed_dt"].iloc[0] == pd.Timestamp("2026-10-25 23:00") and d["net_eq_cr"].min() == -688
