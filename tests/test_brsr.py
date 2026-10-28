"""zen.data.brsr: what a company does, from Section A of its BRSR filing."""
import duckdb

from zen.data import brsr

HEAD = ('<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:in-capmkt="http://www.sebi.gov.in/xbrl/2025-05-31/in-capmkt">')


def ctx(cid, *members):
    seg = "".join(f'<xbrldi:typedMember dimension="in-capmkt:{a}"><in-capmkt:D>{m}</in-capmkt:D></xbrldi:typedMember>' if typed else
                  f'<xbrldi:explicitMember dimension="in-capmkt:{a}">in-capmkt:{m}</xbrldi:explicitMember>' for a, m, typed in members)
    return (f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>'
            f'<xbrli:period><xbrli:instant>2026-03-31</xbrli:instant></xbrli:period>' + (f"<xbrli:scenario>{seg}</xbrli:scenario>" if seg else "") + "</xbrli:context>")


def fact(tag, ref, value):
    return f'<in-capmkt:{tag} contextRef="{ref}">{value}</in-capmkt:{tag}>'


def doc():
    p = [ctx("Main"), ctx("A1", (brsr.ACT_AXIS, "D1", True)),
         ctx("P1", (brsr.PROD_AXIS, "Prod1", True)), ctx("P2", (brsr.PROD_AXIS, "Prod2", True)),
         ctx("Pl_N", ("LocationAxis", "PlantMember", False), ("NationalOrInternationalAxis", "NationalMember", False)),
         ctx("Of_I", ("LocationAxis", "OfficeMember", False), ("NationalOrInternationalAxis", "InternationalMember", False)),
         ctx("E", ("DetailsOfEmployeesAndWorkersIncludingDifferentlyAbledAxis", "PermanentEmployeesMember", False), ("GenderAxis", "GenderMember", False)),
         ctx("E_M", ("DetailsOfEmployeesAndWorkersIncludingDifferentlyAbledAxis", "PermanentEmployeesMember", False), ("GenderAxis", "MaleMember", False)),
         fact("NameOfTheCompany", "Main", "Tata Steel Limited"), fact("DateOfIncorporation", "Main", "1907-08-26"),
         fact("DescriptionOfMainActivity", "A1", "Manufacturing"), fact("DescriptionOfBusinessActivity", "A1", "Metal and metal products"),
         fact("ProductOrServiceSoldByTheEntity", "P2", "Sale of Non-Steel Products"), fact("PercentageOfTotalTurnoverForProductOrServiceSold", "P2", "0.05"),
         fact("ProductOrServiceSoldByTheEntity", "P1", "Sale of Steel Products"), fact("PercentageOfTotalTurnoverForProductOrServiceSold", "P1", "0.93"),
         fact("NICCodeOfProductOrServiceSoldByTheEntity", "P1", "2410"),
         fact("NumberOfLocations", "Pl_N", "78"), fact("NumberOfLocations", "Of_I", "25"),
         fact("NumberOfEmployeesOrWorkersIncludingDifferentlyAbled", "E", "71037"), fact("NumberOfEmployeesOrWorkersIncludingDifferentlyAbled", "E_M", "64211"),
         fact("PercentageOfContributionOfExportsInTheTotalTurnoverOfTheEntity", "Main", "0.08"),
         fact("ABriefOnTypesOfCustomersExplanatoryTextBlock", "Main", "<p>Automotive and   construction.</p>")]
    return (HEAD + "".join(p) + "</xbrli:xbrl>").encode()


def test_section_a_is_read_by_dimension():
    r = brsr.parse_brsr(doc())
    assert r["name"] == "Tata Steel Limited" and r["incorporated"] == "1907-08-26"
    assert r["activities"] == [{"main": "Manufacturing", "business": "Metal and metal products", "share": None}]
    assert [(x["name"], x["share"]) for x in r["products"]] == [("Sale of Steel Products", 0.93), ("Sale of Non-Steel Products", 0.05)]
    assert r["plants_india"] == 78 and r["offices_abroad"] == 25 and r["plants_abroad"] is None
    assert r["employees"] == 71037 and r["workers"] is None and r["exports"] == 0.08
    assert r["customers"] == "Automotive and construction."
    assert brsr.parse_brsr(b"<x/>") is None and brsr.parse_brsr(b"not xml") is None


def test_listing_storage_and_table(tmp_path):
    lst = brsr.listing({"data": [{"symbol": "TATASTEEL", "companyName": "Tata Steel Limited", "fyFrom": 2025, "fyTo": 2026,
                                  "submissionDate": "03-Jun-2026", "revisionDate": "-", "xbrlFile": "https://x/a.xml"},
                                 {"symbol": "X", "xbrlFile": "-"}]})
    assert len(lst) == 1 and lst.iloc[0]["filed"].strftime("%Y-%m-%d") == "2026-06-03" and lst.iloc[0]["revised"] is None
    path = tmp_path / "b.parquet"
    row = brsr.row_for(lst.iloc[0].to_dict(), brsr.parse_brsr(doc()))
    assert brsr.write_parquet([row], path) == 1 and brsr.write_parquet([row], path) == 1      # the same filing once
    con = duckdb.connect()
    assert brsr.rebuild(con, path) == 1
    assert con.execute("SELECT plants_india, products FROM brsr").fetchone()[0] == 78
