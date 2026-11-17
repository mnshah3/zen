

def test_a_document_held_is_known_whichever_host_served_it():
    from jobs.backfill_quarter import doc_key
    a = "https://nsearchives.nseindia.com/corporate/xbrl/BANKING_87442_766963_14012023043623_WEB.xml"
    b = "https://nsearchives.nseindia.in/corporate/xbrl/BANKING_87442_766963_14012023043623_WEB.xml"
    assert doc_key(a) == doc_key(b) and doc_key(a) != doc_key(b.replace("87442", "87443"))
