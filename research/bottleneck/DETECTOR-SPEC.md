# Detection specification — NSE corporate announcements

**One theme survived this pass**, and it survived downgraded: not as a bottleneck-to-equity thesis but as a capex/order-flow detector with a government-monopoly fuel cycle attached.

## Theme key: `nuclear_smr_capex`

**Verdict:** moderate. Import dependence is real and will not close, but the layer where India depends on imports is closed to private capital by statute, and the layer open to private capital is already self-sufficient. The detector therefore catches order flow, not scarcity. Read every hit that way.

**Live status checked 2026-09-07** (three things that change how you tune it):

1. The NPCIL Bharat Small Reactor RFP closed **31 March 2026** after extension from 30 September 2025. Six companies engaged and 16 candidate sites were identified, but on the reporting **Hindalco was the sole final proposer** — Reliance, Adani Power, Tata Power, Jindal Steel & Power and JSW Energy did not submit. A filing from any of those five re-entering is now a high-value hit, and the field is narrower than the earlier research assumed.
2. **No BSR site award or signed private developer contract exists.** NPCIL was still tendering plant engineering and 3D modelling for the 220 MWe BSR with bids due 10 August 2026. The programme is at engineering-design stage, not order stage — the first hard order is still ahead, which is the single event most worth catching.
3. **FDI is not an untouched binary.** The Atomic Energy Commission cleared a nuclear FDI policy in April 2026 and sent it to inter-ministerial consultation, with reporting of a phased structure (possibly 26% initially, reviewable toward 49%). Detect the *notification*, not the announcement — treating "FDI opens" as a surprise in September 2026 is nine months late.
4. SHANTI licensing rules were **not yet notified** at review date, and sources still conflict on whether private uranium exploration and fuel fabrication are permitted. Until notified, a listed company announcing uranium exploration is an anomaly to investigate — including for whether the claim is substantiable — not a discovery.

## Module — save as `zen/monitor/themes.py`

```python
"""
zen.monitor.themes
==================

Keyword specifications for scanning the NSE corporate-announcement archive.

WHAT THIS IS
------------
Each entry in THEMES describes a supply-chain area the platform is watching.
The scanner runs the "include" phrases over the text of a corporate
announcement (title + body + attachment text). A hit means: a human should
read this filing today rather than in six months. The "exclude" phrases
suppress the recurring ways that vocabulary shows up in filings that are not
about a company entering or expanding in the area.

Matching convention assumed by the scanner
------------------------------------------
* Case-insensitive.
* Phrases are LITERAL text, not regex. Pass them through re.escape() and wrap
  in word boundaries, e.g.  re.compile(r"\b" + re.escape(p) + r"\b", re.I).
  No phrase here relies on regex metacharacters; hyphens and digits are
  literal. This keeps the file safe to edit by a non-programmer.
* An exclude hit on the same document suppresses the include hit. Excludes are
  deliberately blunt: in a twenty-year pre-announced government programme,
  false negatives are cheaper than a queue full of intent announcements.

THE CAVEAT, WHICH IS THE MOST IMPORTANT LINE IN THIS FILE
---------------------------------------------------------
A keyword match is a PROMPT TO LOOK. It is never a signal to buy, and this
module must never be wired to anything that sizes, orders or ranks positions.
It says "a company said a specific word today", nothing more. The output of
this scanner is reading material. Whether the announcement means anything
requires knowing the counterparty, the value, whether the thing is a binding
order or an aspiration, and what is already in the price -- none of which a
string match can tell you.

Tuning principle carried over from the research adjudication
-----------------------------------------------------------
Rank a hit by HARDNESS, not by excitement:

    hard   -> licence issued, site allocated/converted, order received with a
              named counterparty AND a stated value, LoI/work order, vendor
              approval granted, certification obtained
    soft    -> MoU, JV "to explore", non-binding term sheet, "evaluating
              opportunities in nuclear", conference/analyst-meet mentions

Soft events in this sector are abundant and close to worthless -- the
programme has been publicly announced for two decades, so intent is free.
The scanner should surface hard events and bury soft ones; the EXCLUDE lists
below do most of that work, and the scoring layer should do the rest.

Why the Sterlite analogy does NOT transfer here
-----------------------------------------------
The Sterlite Technologies AI-data-centre filing (16 June 2025) worked as a
detectable event because the underlying demand shock was sudden and
unanticipated. Indian nuclear is the opposite: slow, statutory, and
pre-announced years in advance. Expect the detector to produce early warning
of ORDER FLOW, not of a surprise. Calibrate expectations accordingly.

Last reviewed: 2026-09-07.
"""

THEMES = {
    "nuclear_smr_capex": {
        "include": [
            # -- programme names and the statutory frame (dateable, specific)
            "bharat small reactor",
            "bharat small modular reactor",
            "bsmr-200",
            "220 mwe pressurised heavy water reactor",
            "shanti act",
            "sustainable harnessing and advancement of nuclear energy",
            "national nuclear energy mission",
            "captive nuclear power plant",

            # -- counterparties whose name in a filing implies a real transaction
            "nuclear power corporation of india",
            "npcil",
            "bhavini",
            "anushakti vidhyut nigam",
            "department of atomic energy",
            "atomic energy regulatory board",
            "nuclear fuel complex",

            # -- hard-event language (licence, order, approval, allocation)
            "letter of intent from nuclear power corporation",
            "npcil vendor registration",
            "approved vendor for nuclear",
            "licence under the shanti act",
            "aerb clearance",
            "asme section iii",
            "asme n-stamp",

            # -- component vocabulary that only nuclear scope uses
            "calandria",
            "end shield",
            "coolant channel assembly",
            "fuelling machine",
            "steam generator for 700 mwe",
            "sa-508",
            "nuclear grade forging",
            "ultra heavy forging",
            "nuclear island package",
            "reactor pressure vessel",
            "heavy water plant",

            # -- fuel-cycle words: a LISTED company using these is an anomaly
            #    worth reading, because most of this layer is reserved to the
            #    state (see "note"). Treat a hit as a question, not a find.
            "uranium exploration",
            "atomic minerals",
            "nuclear fuel fabrication",
            "zircaloy cladding tube",

            # -- the live policy catalyst (already half-telegraphed, see note)
            "foreign direct investment in the nuclear",
        ],
        "exclude": [
            # -- homographs and unrelated uses of the same letters/words
            "bsr code",                    # RBI bank-branch code, very common
            "basic statistical return",
            "smr automotive",              # Motherson subsidiary, not a reactor
            "nuclear family",
            "nuclear medicine",
            "radiopharmaceutical",
            "pet-ct",
            "gamma knife",
            "radiation therapy",
            "nuclear war",
            "nuclear-free",

            # -- soft/intent language: suppress. Intent is free in this sector.
            "memorandum of understanding",
            "non-binding",
            "letter of interest",
            "in-principle understanding",
            "exploring opportunities",
            "evaluating opportunities",
            "intends to explore",
            "proposes to explore",
            "strategic intent",
            "no definitive agreement has been",

            # -- routine filings that merely mention the word
            "clarification on news item",
            "clarification sought by exchange",
            "response to price movement",
            "no undisclosed information",
            "analyst meet",
            "investor presentation",
            "earnings call transcript",
            "schedule of analysts",
            "newspaper publication",
            "board meeting intimation",
            "trading window closure",
            "loss of share certificate",
            "postal ballot",
            "corporate social responsibility",
            "annual report is enclosed",
        ],
        "note": (
            "SURVIVES AS A CAPEX / ORDER-FLOW DETECTOR, NOT AS A BOTTLENECK "
            "THEME. Adjudicated verdict: moderate. The import dependence is "
            "real (~70% of natural uranium against a ~5,400 t/yr PHWR "
            "requirement) but it is not purchasable: the SHANTI Act 2025 "
            "reserves uranium mining, enrichment, reprocessing, high-level "
            "waste and heavy water to the Union Government, and UCIL is "
            "unlisted and wholly state-owned. Where private capital IS "
            "allowed -- forgings, calandrias, end shields, steam generators, "
            "turbine islands, civil works -- India is already self-sufficient "
            "for the indigenous PHWR line, so the binding variable is order "
            "flow (demand), not supply. Read every hit in that light: this "
            "detector catches a capex cycle, not a shortage.\n\n"
            "STATUS AS OF 2026-09-07 (recheck quarterly):\n"
            "  * NPCIL BSR RFP closed 31 March 2026 after extension from 30 "
            "September 2025. Six companies had engaged and 16 candidate sites "
            "were identified across six states, but on the reporting Hindalco "
            "was the SOLE final proposer; Reliance, Adani Power, Tata Power, "
            "Jindal Steel & Power and JSW Energy did not submit. A filing from "
            "any of those five re-entering is therefore a high-value hit.\n"
            "  * No BSR site award or signed private developer contract found. "
            "NPCIL was still tendering plant engineering and 3D modelling for "
            "the 220 MWe BSR with bids due 10 August 2026. The programme is at "
            "engineering-design stage, not order stage -- so the first genuine "
            "hard order is still ahead and is the single event most worth "
            "catching.\n"
            "  * FDI is NOT an untouched binary. The Atomic Energy Commission "
            "cleared an FDI policy for nuclear power in April 2026 (reported "
            "17-19 April 2026) and it went to inter-ministerial consultation, "
            "with reporting of a phased structure -- possibly 26% initially, "
            "reviewable toward 49%. Anyone treating 'FDI opens' as a surprise "
            "catalyst is nine months late. Detect the NOTIFICATION, not the "
            "announcement.\n"
            "  * SHANTI licensing rules were not yet notified at review date. "
            "Sources conflict on whether private uranium exploration and fuel "
            "fabrication are permitted (Section 5 reading reserves mining; "
            "some commentary says exploration and fabrication are open, up to "
            "a notified threshold). Until the rules are notified, treat a "
            "listed company announcing uranium exploration as an ANOMALY TO "
            "INVESTIGATE, including for the possibility that the claim is not "
            "substantiable -- not as a discovery.\n\n"
            "NOT chokepoints, verified negative: zirconium sponge and oxide "
            "(NFC Zirconium Complex, Pazhayakayal, commissioned Nov 2009, "
            "reported running at rated capacity) and domestic heavy forgings / "
            "PHWR primary components (L&T Hazira, BHEL, HCC, MTAR already "
            "supply these). 'zircaloy cladding tube' is retained in the "
            "include list only as a reading trigger for scope changes, not as "
            "evidence of an import gap.\n\n"
            "STILL UNVERIFIED, do not treat as established: nuclear-grade "
            "digital instrumentation and control; large nuclear-grade valves "
            "and reactor coolant pumps for LWR service; TRISO fuel and helium "
            "circulators for the planned 5 MWt HTGR; and private nuclear fuel "
            "fabrication. Each needs a dedicated verification pass.\n\n"
            "EXECUTION RISK DOMINATES. India targeted 20 GWe by 2020 around "
            "2004 and had 8.8 GW across 24 reactors by 2026 (~3% of "
            "generation); the DAE working number was cut to ~22.5 GWe by 2031 "
            "and reaffirmed at that level. 100 GW by 2047 implies roughly "
            "Rs 17.5-20 lakh crore plus a documented nuclear-engineering "
            "talent shortage. Kalpakkam's PFBR, delayed over a decade, is the "
            "standing evidence. Delay risk is asymmetric to the downside, and "
            "the narrative is already consensus even though the price move has "
            "been selective. Nothing here is a recommendation."
        ),
    },
}


# Sub-areas examined in this pass and DELIBERATELY NOT given a theme key.
# Kept in the module so the platform records the negative findings and the
# investor is not re-sold a story that has already been tested and failed.
REJECTED = {
    "uranium_supply": (
        "Real import dependence (~70%), but statutorily reserved to the Union "
        "Government under SHANTI Act 2025; the sole miner UCIL is unlisted. No "
        "listed instrument gets long the uranium shortage. Worse, Indian ore "
        "grades make domestic mining 3-4x the import price -- geology does not "
        "respond to capital or PLI -- and UCIL's roadmap only doubles output "
        "by 2036 while the build-out is front-loaded, so the gap WIDENS "
        "through the investment window. Peak domestic output was 1,062 tU in "
        "2015. Record it as a sovereign RISK factor, not an opportunity."
    ),
    "uranium_enrichment_swu": (
        "Reserved to government and explicitly closed to private participation. "
        "Only Ratnahalli (~25,000 SWU/yr, strategic/research scale); the "
        "industrial-scale Chitradurga SMEF announced in 2011 remains unbuilt. "
        "Binding for enriched-fuel SMR/LWR designs, and uninvestable."
    ),
    "reprocessing_waste_heavy_water": (
        "Spent-fuel management, reprocessing, high-level waste and heavy water "
        "production are reserved to government. A genuine physical constraint "
        "on fleet scaling with no private route."
    ),
    "zirconium_sponge_zircaloy": (
        "NOT a bottleneck. NFC's Zirconium Complex at Pazhayakayal, Tuticorin, "
        "commissioned November 2009, is reported operating at rated capacity. "
        "This was a flagged candidate that did not survive verification."
    ),
    "heavy_forgings_and_reactor_components": (
        "NOT an import bottleneck. L&T Hazira, BHEL, HCC and MTAR already "
        "supply the indigenous PHWR line domestically. The constraint is order "
        "flow, i.e. demand, not supply. Folded into nuclear_smr_capex and "
        "relabelled as a capex-order-flow signal; buying it as a shortage play "
        "is a category error. (The 17,000-tonne Hazira ultra-heavy press for "
        "foreign-LWR-class SA-508 ring forgings is planned, not delivered, and "
        "VVER-class primary components still come from Russian suppliers -- "
        "that residual gap is real but is contingent on foreign-design orders "
        "that have not been placed.)"
    ),
    "fdi_liberalisation_as_catalyst": (
        "Rejected as a SURPRISE catalyst: already half-executed and partially "
        "telegraphed (AEC cleared the policy in April 2026; consultation "
        "ongoing; 26% initial / phased toward 49% reported). Only the formal "
        "notification and the first qualifying transaction remain detectable."
    ),
    "nuclear_liability_reform": (
        "Superseded. Liability is no longer the binding constraint on foreign "
        "participation; the FDI prohibition was, and that is now in motion. Do "
        "not run a detector on liability-amendment language as a live theme."
    ),
}
```

File written to `C:\Users\mnsha\AppData\Local\Temp\claude\C--Users-mnsha-OneDrive-Desktop-Gostack\196360f9-eded-41ee-bd9a-762d776d6462\scratchpad\themes.py` — copy it to `zen/monitor/themes.py`.

## Notes on specific keyword choices

- **`bsr` bare is excluded deliberately.** "BSR code" is an RBI bank-branch identifier and appears constantly in filings; the acronym alone would flood the queue. Same reasoning for bare `smr` — SMR Automotive is a Motherson entity.
- **`npcil` / `bhavini` / `department of atomic energy` are the highest-yield terms**, because in a supplier's filing the counterparty name is what distinguishes a real order from vocabulary.
- **Fuel-cycle terms are included as anomaly triggers, not opportunity triggers.** A listed company announcing uranium exploration is either operating in a legally reserved space, or making a claim that will not survive scrutiny. Both are worth reading; neither is a find.
- **The exclude list suppresses MoUs and "exploring opportunities" aggressively.** This is the correct trade in a twenty-year pre-announced government programme: you will miss some early soft signals, and they were not worth having.

## What was REJECTED, plainly

| Rejected | Why |
|---|---|
| **Uranium supply / U3O8 gap** | Real ~70% import dependence, but reserved to government; UCIL unlisted. Ore grades make domestic mining 3-4x import price, so the gap *widens* through the window. A risk factor, not a purchasable asset. |
| **Enrichment / SWU** | Reserved by statute, explicitly closed to private participation. |
| **Reprocessing, high-level waste, heavy water** | Reserved to government. No private route. |
| **Zirconium sponge / zircaloy** | Not a bottleneck at all — NFC Pazhayakayal running at rated capacity since 2009. Flagged candidate that failed verification. |
| **Heavy forgings, calandrias, steam generators, turbine islands** | India already self-sufficient for the indigenous PHWR line. The constraint is demand, not supply — folded in as capex order flow, not a shortage play. |
| **"FDI opens" as a surprise catalyst** | Half-executed already (AEC cleared April 2026, consultation ongoing). Only the notification remains detectable. |
| **Nuclear liability reform** | Superseded as the binding constraint on foreign capital. Do not run a detector on it. |

Still unverified and not to be treated as established: nuclear-grade I&C, large nuclear valves and reactor coolant pumps, TRISO fuel and helium circulators, and whether private fuel fabrication is actually permitted. Each needs its own pass.

Sources: [World Nuclear News — BSR RFP deadline extended](https://world-nuclear-news.org/articles/deadline-extended-to-allow-wider-participation-in-indian-small-reactor-rfp), [BW Businessworld — Hindalco sole proposal](https://www.businessworld.in/article/hindalco-submits-sole-proposal-for-npcil-s-bharat-small-reactor-plan-616959), [Construction World — NPCIL engineering/3D design tender](https://www.constructionworld.in/policy-updates-and-economic-news/npcil-seeks-bids-for-engineering-and-3d-design-of-bharat-small-reactors/95492), [Business Standard — AEC clears nuclear FDI policy](https://www.business-standard.com/india-news/nuclear-fdi-policy-cleared-sent-for-consultations-dae-official-126041701043_1.html), [Business Standard — up to 49% FDI considered](https://www.business-standard.com/industry/news/india-may-allow-up-to-49-foreign-investment-in-nuclear-power-plants-125042500647_1.html), [JSA — SHANTI Act 2025 analysis](https://www.jsalaw.com/energy-power-hydrocarbon/shanti-act-2025-calibrated-liberalization-in-nuclear-energy-to-secure-innovation-decarbonization-diversification-of-energy-sources/), [CMS IndusLaw — SHANTI Act relevance for private stakeholders](https://cms-induslaw.com/en/ind/publication/shanti-act-2025-nuclear-power-projects-relevance-for-private-stakeholders), [PIB — SHANTI Act press note](https://www.pib.gov.in/PressNoteDetails.aspx?id=156593&NoteId=156593&ModuleId=3&reg=3&lang=2), [World Nuclear Association — Nuclear Power in India](https://world-nuclear.org/information-library/country-profiles/countries-g-n/india)