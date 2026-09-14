"""Filing categories from NSE's own subtype, not from guessing at the text.

WHY THE REGEX HAD TO GO

Every filing's `subject` begins with NSE's own classification, followed by a
colon. That prefix is present on 783,505 of 783,510 rows -- 99.999% -- and it is
what the exchange itself calls the filing. The previous categoriser ignored it
and pattern-matched the free text instead, which produced an `orders` bucket
that was roughly a quarter genuine order wins and a quarter regulatory
penalties. Those have opposite signs. Averaging them and calling the result an
edge is how a study reaches a confident conclusion about nothing.

TWO VOCABULARIES, AND WHY BOTH ARE MAPPED

NSE changed these labels between 2023 and 2024. "Bagging orders/contract"
appears only in 2022; "Bagging/Receiving of orders/contracts" only from 2024.
They are the same event. Mapping only the current names would make 2022 look
empty of order wins, and a study comparing periods would be comparing its own
taxonomy rather than the market.

  2022 only                                    2024 onward
  Bagging orders/contract                      Bagging/Receiving of orders/contracts
  Awarding orders/contract                     Awarding of order(s)/contract(s)
  Capacity addition/product launch             Capacity addition, Product launch
  Sale or disposal of unit/division            Sale or disposal
  Litigations/Disputes/Regulatory actions      Action(s) taken or orders passed

THE HOLE IN 2023, AND WHY IT IS NOT WORTH FILLING

Order subtypes appear 355 times in 2022 under the old names, ZERO times in 2023,
and 2,865 times from 2024-09-23 under the new ones. The 2023 filings exist; the
exchange simply did not classify them.

They are partly recoverable from free text, and that was tested rather than
assumed. A pattern tuned against the labelled era reaches about 64% recall at
roughly 90% precision, which sounds usable until you ask WHICH filings it
recovers: the ones whose subject line carries descriptive prose. That is a
property of the filer's house style, not of the event. The rescued 2023 bucket
covers 165 symbols against the 283 that file order wins in a labelled year, with
ten symbols contributing 27% of the rows. A backtest on it would be measuring
which companies write informative subject lines.

So the rescue exists for the alert email, where a wrong row costs the reader
thirty seconds, and is kept out of anything quantitative. The factor's order
bucket is the labelled one. `USABLE_FROM` records the first trustworthy year
per category so a study cannot quietly reach past it.

SIGN

Each category carries an expected direction. It is not used to score anything --
it exists so that two categories which move prices in opposite directions can
never end up in the same bucket again.
"""

from __future__ import annotations

import re
from datetime import date

# --------------------------------------------------------------------------
# Categories, their expected direction, and the first year the data supports.
# --------------------------------------------------------------------------

SIGN = {
    # Each sign below is measured, not assumed. day-0 move against a
    # liquidity-matched control, 3 sessions, corporate-action adjusted:
    #   orders +0.854pp (n=1,943)   expansion +0.434pp (n=978)
    #   contraction -0.175pp (n=201)
    # Orders then GIVE BACK 0.467pp over the following three sessions, and
    # only 41.4% are up against 46.5% of controls. The pop is real and so is
    # the fade; anything acting on an order win has to choose which it is
    # trading.
    "orders": +1,          # winning work
    "expansion": +1,       # adding capacity, starting production
    "contraction": -1,     # closing, halting, disrupting
    # MEASURED, not asserted. Over 3,356 events from 2025, the day-0 move
    # against a liquidity-matched control is -0.036pp, which is flat. Penalties,
    # litigation and insolvency filings do not move prices measurably, so the
    # -1 this carried was an assumption the data does not support. See
    # jobs/study_category_signs.py.
    "regulatory": 0,
    "licenses": 0,         # one label covers grants and withdrawals, 17 to 1
    "mna": 0,              # direction depends entirely on terms
    "capital": 0,          # raising money dilutes and funds growth at once
    "ratings": 0,          # the subtype does not say which way
    "results": 0,
    "guidance": 0,
    "exchange_query": 0,   # arrives AFTER the move, so it confirms not causes
    "governance": 0,
    "corp_action": 0,
    "routine": 0,
    "other": 0,
}

# First year each category can be trusted. Before this, absence means "not
# labelled", not "did not happen".
#
# THE CUTOVER IS A SINGLE DAY, NOT A RAMP. Every new-vocabulary subtype begins
# on 2024-09-23 -- orders, capacity addition, regulatory actions, all of them.
# Filings under ANY new label between 2024-01-01 and 2024-09-22: zero. An
# earlier version of this table said 2024, which let a study read eight months
# of structural zeros as "no orders happened". These are 2025 because 2024 is
# three months of data attached to nine months of nothing.
#
# There is also a cross-sectional problem a year cannot express. In 2024, 107
# symbols filed order wins under the new labels and 223 filed them under vague
# ones, overlapping on only 55. Sixty-one per cent of that year's order-filing
# universe is invisible to any subtype mapping, so 2024 is not merely thin, it
# is selected.
#
# guidance is 2023 because its main subtype is absent from March to November
# 2022, when the same events were filed under other labels.
USABLE_FROM = {
    "orders": 2025,
    "expansion": 2025,
    "contraction": 2025,
    "regulatory": 2025,
    "licenses": 2025,
    "mna": 2022,
    "capital": 2022,
    "ratings": 2022,
    "results": 2022,
    "guidance": 2023,
    "exchange_query": 2022,
    "governance": 2022,
    "corp_action": 2022,
    "routine": 2022,
    "other": 2022,
}


def _norm(s: str) -> str:
    """Collapse the spelling variants NSE uses for the same label.

    "General Updates" and "General updates" are 33,194 and 17,834 rows of the
    same thing. So are "Loss of Share Certificates" and "Loss of share
    certificate". Case, trailing plurals and punctuation spacing all vary.
    """
    s = (s or "").strip().lower()
    s = re.sub(r"[‘’']", "", s)
    s = re.sub(r"[/,\-&()\.]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Singularise the last word only; "orders contracts" and "order contract"
    # are the same label and nothing here depends on plurality.
    return re.sub(r"s\b", "", s)


# Subtype -> category. Written out in full rather than pattern-matched, so the
# mapping can be read and argued with. Anything absent falls to "other" AND is
# reported by unmapped(), because a new NSE subtype must surface rather than
# quietly join the noise.
_RAW: dict[str, tuple[str, ...]] = {
    "orders": (
        "Bagging/Receiving of orders/contracts",
        "Awarding of order(s)/contract(s)",
        "Bagging orders/contract",
        "Awarding orders/contract",
    ),
    "expansion": (
        "Capacity addition",
        "Capacity addition/product launch",
        "Commencement of commercial production/operations",
        "Product launch",
        "Adoption of new line(s) of business",
        "Arrangements for strategic, technical, manufacturing, or marketing tie up",
        "Commencement/Postponement of Operations",
    ),
    "contraction": (
        "Postponement of commercial production/operations",
        "Closure of operations",
        "Closure of operations of any unit/division",
        "Disruption of Operations",
        "Disruption of operations",
        "Strikes/Lockouts/Disturbances",
        "Rescission/termination(s)",
        "Amendment/Termination of awards/contracts",
    ),
    "regulatory": (
        "Action(s) taken or orders passed",
        "Action(s) initiated or orders passed",
        "Litigations/Disputes/Regulatory actions",
        "Pendency of Litigation(s)/dispute(s) or the outcome impacting the Company",
        "Delay/default in the payment of fines/penalties/dues etc. to authority",
        "Fraud/Default/Arrest",
        "Frauds/Default by employees",
        "Defaults on Payment of Interest/Principal",
        "One Time Settlement",
        "One time settlement",
        "Corporate Insolvency Resolution Process",
        "CIRP - others",
        "CIRP - Commencement",
        "CIRP - Committee meeting updates",
        "CIRP - Filing of application",
        "CIRP - Approval of Resolution Plan",
        "Liquidation",
        "Effect(s) on listed entity due to changed regulatory framework applicable",
        "CIRP - Filing of Resolution Plan",
        "CIRP - Change in Resolutional Professional",
        "CIRP - Revocation/rejection",
        "Initiation of Forensic Audit",
        "Final forensic audit report",
        "Corporate Debt Restructuring",
        "Public Announcement - Delisting",
        "Effects - Change in regulatory framework",
        "One Time Settlement-XBRL",
    ),
    "licenses": (
        "Granting/withdrawal/surrender/cancellation/suspension of key licenses/ regulatory approvals",
        "Withdrawal/Surrender/Cancellation or suspension of licenses/ regulatory approvals",
        "Grant of licenses/regulatory approvals",
    ),
    "mna": (
        "Acquisition",
        "Amalgamation/Merger",
        "Scheme of Arrangement",
        "Demerger",
        "Open Offer",
        "Public Announcement-Open Offer",
        "Post Offer Public Announcement",
        "Sale or disposal",
        "Sale or disposal of unit/ division/subsidiary",
        "Diversification/Disinvestment",
        "Restructuring",
        "Other Restructuring",
        "Corp Restructuring - others",
        "Update-Acquisition/Scheme/Sale/Disposal/Reg30-XBRL",
        "Slump Sale",
        "Joint Venture",
        "Sale or disposal-XBRL",
        "Voluntary Delisting",
    ),
    "capital": (
        "Allotment of Securities",
        "Issue of Securities",
        "Rights Issue",
        "Preferential issue",
        "Preferential Issue",
        "Qualified Institutional Placement",
        "Offer for sale",
        "Conversion",
        "Increase in Authorised Capital",
        "Debentures",
        "Share Warrants",
        "Issuance/changes in Capital-Others",
        "Redemption",
        "Options to purchase securities",
        "Preference Shares",
        "Capital Reduction",
        "Institutional Placement Programme",
        "FCCBs",
        "FCCB/ FCEB",
        "Follow-on public issue",
        "Global Depository Receipts",
        "Withdrawal of Rights Issue",
        "Alteration Of Capital and Fund Raising-XBRL",
        "Payouts- others",
    ),
    "ratings": (
        "Credit Rating",
        "Credit Rating- Revision",
        "Credit Rating- New",
        "Credit Rating- Others",
    ),
    "results": (
        "Financial Result Updates",
        "Financial Results Updates",
        "Integrated Filing- Financial",
        "Clarification - Financial Results",
        "Clarification- Financial Results",
        "Reply to Clarification- Financial results",
        "Reply to Clarification Sought- Financial Results",
        "Limited Review Report",
        "Publish Audited Results",
        "Auditor's report",
        "Statement on Impact of Audit Qualifications",
        "Reasons for Delayed/Non-submission of Financial Results",
        "Declaration for audit reports with unmodified opinion(s)",
        "Disclosure of Annual financial information (if submitted as part of Annual Report)",
        "Consolidated Result Updates - IFRS",
        "Audit Qualifications/Comments",
        "Voluntary Revision of Financial statements or Report",
        "Disclosure of Half yearly financial information (if submitted as part of Half-Yearly Report)",
        "Disclosure of Valuation report",
    ),
    "guidance": (
        "Investor Presentation",
        "Transcript of Analysts/Institutional Investor Meet/Con. Call",
        "Monthly Business Updates",
        "Analysts/Institutional Investor Meet/Con. Call Updates",
        "Recording of Analysts/Institutional Investor Meet/Con. Call",
    ),
    "exchange_query": (
        "Spurt in Volume",
        "Price movement",
        "News Verification",
        "Rumour Verification - Regulation 30(11)",
        "Clarification",
        "Reply to Clarification Sought",
        "Clarification sought",
    ),
    "governance": (
        "Appointment", "Resignation", "Cessation", "Retirement", "Demise",
        "Change in Management", "Change in Director(s)", "Re-appointment",
        "Change in designation", "Change in Auditors",
        "Resignation of Independent director", "Resignation of Statutory Auditor",
        "Resignation of Director/KMP/SMP",
        "Change in Company Secretary/Compliance Officer",
        "Change in Directors/ Key Managerial Personnel/ Auditor/ Compliance Officer/ Share Transfer Agent",
        "Related Party Transaction", "Related Party Transactions",
        "Integrated Filing- Governance",
        "Giving guarantees/indemnity/ becoming a surety for third party",
        "Giving of guarantee/indemnity or becoming surety",
        "Appointment of Company Secretary and Compliance Officer",
        "Disc. under Reg.30 of SEBI (SAST) Reg.2011",
    ),
    "corp_action": (
        "Dividend", "Dividend Updates", "Date of payment of dividend",
        "Bonus", "Stock split", "Split of shares",
        "Record Date", "Revised Record date", "Cancellation of Record date",
        "Book Closure", "Revised Book Closure", "Cancellation of Book closure",
        "Buyback", "Buyback - others", "Buyback - Tender offer",
        "Buyback - Open Market", "Closure of Buy Back",
        "Daily Buy Back of securities", "Post Buyback Public Announcement",
        "Public Announcement - Buyback of Shares",
        "Disclosure of record date for purpose of distribution",
        "Forfeiture", "ESOP/ESOS/ESPS", "ESOP/ESPS/SBEB Scheme",
        "Date of payment of Interest/Principal",
        "Confirmation of payment of Interest/Principal",
        "Interest Rates Updates",
        "Closure of Buyback",
        "Allotment of ESOP/ESPS",
        "Cancellation of Dividend",
        "Suspension of Trading",
        "Revocation of Suspension of Securities",
        "Delisting",
    ),
    "routine": (
        "Copy of Newspaper Publication", "Newspaper Advertisements",
        "Loss of Share Certificates", "Loss of share certificate",
        "Issue of Duplicate Share Certificate",
        "Trading Window", "Closure of trading window",
        "Certificate under SEBI (Depositories and Participants) Regulations, 2018",
        "Shareholders meeting", "Annual General Meeting", "Extra Ordinary Meeting",
        "Postal Ballot", "Notice Of Shareholders Meetings-XBRL",
        "NCLT/ Court Convened Meeting", "Extension of Annual General Meeting",
        "Outcome of Board Meeting", "Board Meeting Intimation",
        "Board Meeting Adjourned", "Board meeting Cancelled",
        "Committee Meeting Updates", "Outcome of committee meeting",
        "Schedule of Analysts/Institutional Investor Meet/Con. Call",
        "Disclosure under SEBI Takeover Regulations",
        "Annual Secretarial Compliance Report",
        "Quarterly Compliance Report on Corporate governance - within 21 days from the end of the quarter",
        "Code of Conduct under SEBI(PIT) Reg., 2015",
        "Code of conduct under SEBI (PIT) Regulations",
        "Disclosure under SEBI (PIT) Reg 2015",
        "Trading Plan under PIT", "Insider Trading - Others",
        "Structural Digital Database",
        "Business Responsibility & Sustainability Report (BRSR)",
        "Address Change", "Name Change", "Name & Symbol Change",
        "Name and Symbol Change", "Symbol Change of company",
        "Amendment to AOA/MOA", "Amendment(s)", "Alteration/revision(s)",
        "Registrar & Share Transfer Agent Update",
        "E-mail ID for Investor's Grievance Redressal",
        "Communication to shareholders as per Reg 30",
        "Corrigendum", "Addendum", "Withdrawal", "Cancellation",
        "Annual Disclosure", "Incorporation",
        "Trading Plan under SEBI (PIT) Regulations",
        "Trading Plan under SEBI (PIT) Reg., 2015",
        "Notice of Unitholder meetings",
        "Outcome of Unitholder meetings",
        "Adjournment/Reschedule/Postpone",
        "Intimation of Board Meeting",
        "Outcome of Board Meeting-XBRL",
        "Change in Financial Year",
        "Extension of Financial Year",
        "Incorporation-XBRL",
        "Disclosure of all complaints including SCORES complaints received by the InvIT on a quarterly basis",
        "Utilisation of Funds",
        "Monitoring Agency Report",
        "Statement of deviation(s) or variation(s) under Reg. 32",
    ),
}

# subtype (normalised) -> category
TYPE_TO_CATEGORY: dict[str, str] = {
    _norm(name): cat for cat, names in _RAW.items() for name in names
}

# Labels that are too vague to carry a signal. Kept separate from "unmapped"
# so that a genuinely new NSE subtype is distinguishable from one deliberately
# parked. "Updates" alone is 72,703 rows and says nothing at all.
VAGUE = {_norm(x) for x in (
    "Disclosure of other UPSI/material event",
    "Disclosure of material issue",
    "Updates", "General Updates", "General updates", "Others",
    "Press Release", "Press Release (Revised)", "Agreements",
    "Memorandum of Understanding/Agreements",
    "Agreements/Contracts/Arrangements/ MOU's PARA A",
    "Agreements/Contracts/Arrangements/ MOU's PARA B",
    "Agreements,Contracts,Arrangements,MOU-XBRL",
)}


# --------------------------------------------------------------------------
# The one place a body test is justified.
# --------------------------------------------------------------------------
#
# "Financial Result Updates" runs 2022-01 to 2025-03 and then stops dead: 3,849
# / 7,782 / 8,225 / 2,158 / ZERO across 2022-2026. From April 2025 NSE folds
# quarterly results into "Outcome of Board Meeting", which is otherwise routine
# meeting housekeeping. Left alone, the results category simply ends in April
# 2025 and nothing raises.
#
# Matching a body here is safe in a way the order regex never was, and the
# difference is whose prose it is. The order regex matched text COMPANIES wrote,
# which varies by house style. This matches NSE's own generated template.
# Measured on 2023-24, where both labels coexist: 100.0% recall on 16,007
# "Financial Result Updates" rows, and it fires on 4 of 16,177 "Outcome of Board
# Meeting" rows in two years, a 0.02% false rate.
#
# The second alternative catches 2022 H2 phrasing, where "Financial Result
# Updates" is literally zero from August to November 2022 and coverage falls to
# 5% of the quarter. Without it, results cannot be used before 2023 either.
RESULTS_BODY = re.compile(
    r"submitted to the exchange,?\s+the\s+.{0,40}?financial result"
    r"|(?:considered|approved|consider)[^.]{0,60}?"
    r"(?:financial result|financial statement)",
    re.I)

# Subtypes that are results ONLY when the body says so. Everything else filed
# under them is genuine meeting housekeeping.
RESULTS_BY_BODY = {_norm(x) for x in
                   ("Outcome of Board Meeting", "Outcome of Board Meeting-XBRL")}


def subtype(subject: str | None) -> str:
    """NSE's own label, the part before the first colon."""
    if not subject:
        return ""
    head, sep, _ = subject.partition(": ")
    return head.strip() if sep else ""


def body(subject: str | None) -> str:
    """Everything after NSE's label."""
    _, sep, rest = (subject or "").partition(": ")
    return rest if sep else ""


def categorise(subject: str | None) -> str:
    st = _norm(subtype(subject))
    if not st:
        return "other"
    if st in RESULTS_BY_BODY and RESULTS_BODY.search(body(subject) or ""):
        return "results"
    if st in VAGUE:
        return "other"
    return TYPE_TO_CATEGORY.get(st, "other")


def usable(category: str, when: date) -> bool:
    """Is this category meaningful on this date, or merely unlabelled?

    Absence of an order filing in 2023 does not mean no company won an order.
    It means NSE was not labelling them. A study that treats the two the same
    concludes the signal decayed when the taxonomy changed.
    """
    return when.year >= USABLE_FROM.get(category, 2022)


def unmapped(subjects) -> dict[str, int]:
    """Subtypes seen in the data that this module does not know about.

    Run it after every refresh. A silently growing "other" bucket is how a new
    NSE label disappears from a study without anyone noticing.
    """
    from collections import Counter
    out: Counter = Counter()
    for s in subjects:
        st = subtype(s)
        n = _norm(st)
        if n and n not in TYPE_TO_CATEGORY and n not in VAGUE:
            out[st] += 1
    return dict(out.most_common())
