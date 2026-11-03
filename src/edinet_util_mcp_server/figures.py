"""Key figures from an annual report's type-5 CSV (XBRL converted by EDINET).

The ZIP's `XBRL_TO_CSV/` holds one file per report part; the report itself is the
`jpcrp…` file (the `jpaud…` files are the audit reports). Despite the name the
files are tab-separated UTF-16 with every value quoted, one row per fact:

    要素ID, 項目名, コンテキストID, 相対年度, 連結・個別, 期間・時点, ユニットID, 単位, 値

Traps this module handles:
- Consolidation is read from the context ID (`…_NonConsolidatedMember`), never
  from the 連結・個別 column, which says "その他" for consolidated IFRS figures.
- The 5-year table (経営指標等) uses `…SummaryOfBusinessResults` elements, or
  `…KeyFinancialData` in newer filings (e.g. `jpcrp_cor:RevenueKeyFinancialData`), with
  `IFRS`/`USGAAP`/`JMIS` variants and company-specific extensions (e.g.
  `jpcrp030000-asr_E00436-000:TotalEquityIFRSSummaryOfBusinessResults`).
- Element names are not always what they say: the IFRS
  `EquityToAssetRatioIFRSSummaryOfBusinessResults` is book value *per share*
  (unit JPYPerShares). Every field is therefore matched on element and unit.
- A consolidated filer reports dividends, payout ratio and shares issued only in
  the non-consolidated half; those are company-level and read from either half.
- The top line has no common element (売上高, 売上収益, 営業収益, 経常収益, …),
  so revenue is the first 5-year-table amount whose label names a revenue.
- Japanese GAAP 5-year tables have no operating profit; it and depreciation come
  from the statements, which cover only the current and prior year.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date

_CONTEXT = re.compile(
    r"^(?:CurrentYear|Prior(?P<prior>[1-4])Year)(?P<kind>Duration|Instant)"
    r"(?P<noncons>_NonConsolidatedMember)?$"
)
_SUMMARY_SUFFIXES = ("SummaryOfBusinessResults", "KeyFinancialData")
_STANDARD_SUFFIX = re.compile(r"(IFRS|USGAAP|JMIS)$")
_REVENUE_LABEL = re.compile(
    r"^(売上高|売上収益|営業収益|経常収益|営業総収入|完成工事高|純営業収益|営業収入|事業収益|収益)"
)

# Revenue elements whose label column can be blank, with the label to show.
REVENUE_NAMES = {
    "NetSales": "売上高",
    "Revenue": "売上収益",
    "Revenue2": "売上収益",
    "NetSalesOfCompletedConstructionContracts": "完成工事高",
    "NetSalesAndOperatingRevenue": "売上高及び営業収益",
    "BusinessRevenue": "事業収益",
    "OperatingRevenue": "営業収益",
    "OperatingRevenues": "営業収益",
    "OperatingRevenue1": "営業収益",
    "OperatingRevenue2": "営業収益",
    "InsurancePremiumsAndOtherIncome": "保険料等収入",
    "InsurancePremiumsAndOthers": "保険料等収入",
    "PremiumAndOtherIncome": "保険料等収入",
    "InsuranceRevenue": "保険収益",
    "GrossSales": "総売上高",
}

JPY = "JPY"
PURE = "pure"
PER_SHARE = "JPYPerShares"
SHARES = "shares"


@dataclass(frozen=True)
class Fact:
    element: str
    label: str
    context: str
    unit: str
    value: str

    @property
    def local_name(self) -> str:
        return self.element.rsplit(":", 1)[-1]

    @property
    def summary_base(self) -> str | None:
        """`NetSales` for `jpcrp_cor:NetSalesIFRSSummaryOfBusinessResults`, else None."""
        name = self.local_name
        for suffix in _SUMMARY_SUFFIXES:
            if name.endswith(suffix):
                return _STANDARD_SUFFIX.sub("", name[: -len(suffix)])
        return None


@dataclass(frozen=True)
class _Field:
    names: tuple[str, ...]
    unit: str
    kind: str  # Duration | Instant
    company_level: bool = False


# Matched against the summary base name, in order of preference.
SUMMARY_FIELDS: dict[str, _Field] = {
    "operating_income": _Field(("OperatingIncomeLoss", "OperatingProfitLoss"), JPY, "Duration"),
    "ordinary_income": _Field(("OrdinaryIncomeLoss",), JPY, "Duration"),
    "profit_before_tax": _Field(
        ("ProfitLossBeforeTax", "IncomeLossBeforeIncomeTaxes", "ProfitLossBeforeIncomeTaxes"),
        JPY, "Duration"),
    "net_income": _Field(
        ("ProfitLossAttributableToOwnersOfParent", "NetIncomeLossAttributableToOwnersOfParent",
         "NetIncomeLoss", "ProfitLoss"), JPY, "Duration"),
    "comprehensive_income": _Field(
        ("ComprehensiveIncomeAttributableToOwnersOfParent", "ComprehensiveIncome"),
        JPY, "Duration"),
    "net_assets": _Field(
        ("NetAssets", "EquityAttributableToOwnersOfParent", "TotalEquity"), JPY, "Instant"),
    "total_assets": _Field(("TotalAssets",), JPY, "Instant"),
    "equity_ratio": _Field(("RatioOfOwnersEquityToGrossAssets", "EquityToAssetRatio"),
                           PURE, "Instant"),
    "roe": _Field(("RateOfReturnOnEquity",), PURE, "Duration"),
    "eps": _Field(("BasicEarningsLossPerShare", "BasicEarningsPerShare"), PER_SHARE, "Duration"),
    "bps": _Field(("NetAssetsPerShare", "EquityAttributableToOwnersOfParentPerShare",
                   "EquityToAssetRatio"), PER_SHARE, "Instant"),
    "per": _Field(("PriceEarningsRatio",), PURE, "Duration"),
    "operating_cf": _Field(("CashFlowsFromUsedInOperatingActivities",
                            "NetCashProvidedByUsedInOperatingActivities"), JPY, "Duration"),
    "investing_cf": _Field(("CashFlowsFromUsedInInvestingActivities",
                            "NetCashProvidedByUsedInInvestingActivities"), JPY, "Duration"),
    "financing_cf": _Field(("CashFlowsFromUsedInFinancingActivities",
                            "NetCashProvidedByUsedInFinancingActivities"), JPY, "Duration"),
    "cash": _Field(("CashAndCashEquivalents",), JPY, "Instant"),
    "dps": _Field(("DividendPaidPerShare",), PER_SHARE, "Duration", company_level=True),
    "payout_ratio": _Field(("PayoutRatio",), PURE, "Duration", company_level=True),
    "issued_shares": _Field(("TotalNumberOfIssuedShares",), SHARES, "Instant",
                            company_level=True),
}

# Statement elements, used when the 5-year table has no such line. Current and
# prior year only.
STATEMENT_FIELDS: dict[str, tuple[str, ...]] = {
    "revenue": ("jppfs_cor:NetSales", "jpigp_cor:RevenueIFRS", "jpigp_cor:NetSalesIFRS",
                "jpigp_cor:Revenue2IFRS"),
    "operating_income": ("jppfs_cor:OperatingIncome", "jpigp_cor:OperatingProfitLossIFRS"),
    "depreciation": ("jppfs_cor:DepreciationAndAmortizationOpeCF",
                     "jpigp_cor:DepreciationAndAmortizationOpeCFIFRS",
                     "jppfs_cor:DepreciationOpeCF"),
}

STATEMENT_REVENUE_LABELS = {
    "jppfs_cor:NetSales": "売上高",
    "jpigp_cor:RevenueIFRS": "売上収益（IFRS）",
    "jpigp_cor:NetSalesIFRS": "売上高（IFRS）",
    "jpigp_cor:Revenue2IFRS": "売上収益（IFRS）",
}

YEAR_FIELDS = ("revenue", *SUMMARY_FIELDS, "depreciation")


@dataclass
class YearFigures:
    offset: int  # 0 = the report's fiscal year, 1 = the year before, ...
    period_end: str | None
    values: dict[str, float | None] = field(default_factory=dict)


@dataclass
class AnnualFigures:
    accounting_standard: str | None  # Japan GAAP | IFRS | US GAAP | JMIS
    consolidated: bool
    fiscal_year_start: str | None
    fiscal_year_end: str | None
    revenue_label: str | None
    market: str | None
    employees: int | None
    issued_shares: int | None
    treasury_shares: int | None
    years: list[YearFigures]

    @property
    def basis(self) -> str:
        return "consolidated" if self.consolidated else "non-consolidated"

    @property
    def latest(self) -> YearFigures | None:
        return self.years[0] if self.years and self.years[0].offset == 0 else None

    def market_cap(self) -> int | None:
        """PER × EPS × (issued − treasury) at the fiscal year end; None without a PER."""
        latest = self.latest
        if not latest or not self.issued_shares:
            return None
        per, eps = latest.values.get("per"), latest.values.get("eps")
        if not per or not eps or per <= 0 or eps <= 0:
            return None
        return round(per * eps * (self.issued_shares - (self.treasury_shares or 0)))


def report_csv_text(zip_bytes: bytes) -> str:
    """The annual report's own CSV from a type-5 ZIP, decoded."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = [n for n in archive.namelist()
                 if n.lower().endswith(".csv") and n.rsplit("/", 1)[-1].startswith("jpcrp")]
        if not names:
            raise ValueError("No jpcrp CSV in the archive.")
        return archive.read(min(names)).decode("utf-16")


def parse_facts(text: str) -> list[Fact]:
    rows = csv.reader(io.StringIO(text), delimiter="\t")
    next(rows, None)
    return [Fact(element=r[0], label=r[1], context=r[2], unit=r[6], value=r[8])
            for r in rows if len(r) >= 9]


def extract(facts: list[Fact], *, consolidated: bool | None = None) -> AnnualFigures:
    """The report's figures, on its own basis unless `consolidated` overrides it.

    The override serves a filer that has just started consolidating: one
    consolidated year, but a full non-consolidated history in the same table.
    """
    dei = {f.local_name: f.value for f in facts if f.element.startswith("jpdei_cor:")}
    standard = _text(dei.get("AccountingStandardsDEI"))
    fiscal_end = _text(dei.get("CurrentFiscalYearEndDateDEI"))
    previous_end = _text(dei.get("PreviousFiscalYearEndDateDEI"))

    summary = [(f, m) for f in facts
               if f.summary_base is not None and (m := _CONTEXT.match(f.context))]
    # The report's own flag, not the presence of consolidated facts: a filer that
    # stopped consolidating still reports earlier consolidated years.
    if consolidated is None:
        flag = _text(dei.get("WhetherConsolidatedFinancialStatementsArePreparedDEI"))
        consolidated = (flag == "true" if flag in ("true", "false")
                        else any(not m["noncons"] for _, m in summary))

    years: dict[int, YearFigures] = {}

    def put(offset: int, name: str, value: float | None) -> None:
        if value is None:
            return
        year = years.setdefault(offset, YearFigures(offset, _period_end(offset, fiscal_end,
                                                                         previous_end)))
        year.values.setdefault(name, value)

    def halves(company_level: bool) -> tuple[bool, ...]:
        # Which halves to read, in order (True = the non-consolidated half). A
        # consolidated filer's company-level lines are only in the non-consolidated
        # half. A non-consolidated filer uses the non-consolidated half, else the
        # plain contexts, but never both for one line.
        if not consolidated:
            return (True, False)
        return (False, True) if company_level else (False,)

    revenue_label = None
    for noncons in halves(False):
        revenue_base = None
        for fact, match in summary:
            if (bool(match["noncons"]) != noncons or match["kind"] != "Duration"
                    or fact.unit != JPY):
                continue
            base = fact.summary_base
            if revenue_base is None and (_REVENUE_LABEL.match(fact.label)
                                         or base in REVENUE_NAMES):
                revenue_base = base
                revenue_label = fact.label.split("、")[0] or REVENUE_NAMES.get(base, base)
            if base == revenue_base:
                put(_offset(match), "revenue", _number(fact.value))
        if revenue_base:
            break

    for name, spec in SUMMARY_FIELDS.items():
        for candidate in spec.names:
            found = False
            for noncons in halves(spec.company_level):
                for fact, match in summary:
                    if (fact.summary_base == candidate and fact.unit == spec.unit
                            and match["kind"] == spec.kind
                            and bool(match["noncons"]) == noncons):
                        value = _number(fact.value)
                        if value is not None:
                            put(_offset(match), name, value)
                            found = True
                if found:
                    break
            if found:
                break

    for name, elements in STATEMENT_FIELDS.items():
        matched = False
        for element in elements:
            for noncons in halves(False):
                for fact in facts:
                    match = _CONTEXT.match(fact.context)
                    if (fact.element != element or not match or match["kind"] != "Duration"
                            or bool(match["noncons"]) != noncons or _offset(match) > 1):
                        continue
                    year = years.get(_offset(match))
                    value = _number(fact.value)
                    if value is not None and (year is None or name not in year.values):
                        put(_offset(match), name, value)
                        matched = True
                if matched:
                    break
            if matched:
                if name == "revenue" and revenue_label is None:
                    revenue_label = STATEMENT_REVENUE_LABELS[element]
                break

    return AnnualFigures(
        accounting_standard=standard,
        consolidated=consolidated,
        fiscal_year_start=_text(dei.get("CurrentFiscalYearStartDateDEI")),
        fiscal_year_end=fiscal_end,
        revenue_label=revenue_label,
        market=_market(facts),
        employees=_employees(facts, consolidated),
        issued_shares=_issued_shares(facts, years),
        treasury_shares=_int(_first(facts, "jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc",
                                    "CurrentYearInstant")),
        years=[years[k] for k in sorted(years)],
    )


def _offset(match: re.Match[str]) -> int:
    return int(match["prior"] or 0)


def _period_end(offset: int, current: str | None, previous: str | None) -> str | None:
    if offset == 0:
        return current
    if offset == 1 and previous:
        return previous
    if not current:
        return None
    # Earlier years: the same month and day, `offset` years back. Wrong only for a
    # filer that changed its year end within the five years.
    end = date.fromisoformat(current)
    try:
        return end.replace(year=end.year - offset).isoformat()
    except ValueError:  # 29 February
        return end.replace(year=end.year - offset, day=28).isoformat()


def _market(facts: list[Fact]) -> str | None:
    """The listing markets, normalised: `東京証券取引所プライム市場、名古屋証券取引所メイン市場`.

    Filers write them freely: with spaces, brackets around the segment, a
    leading 株式会社, or several exchanges run together in one value.
    """
    names: list[str] = []
    for fact in facts:
        if not fact.local_name.startswith("NameOfFinancialInstrumentsExchangeOnWhichSecurities"):
            continue
        value = _text(fact.value)
        if not value:
            continue
        value = re.sub(r"\s+", "", value).replace("株式会社", "")
        value = re.sub(r"[（(]([^）)]*市場)[）)]", r"\1", value)
        value = re.sub(r"(?<!^)(?<![、,])(東京|名古屋|福岡|札幌|大阪)(証券取引所)", r"、\1\2", value)
        for part in re.split(r"[、,]+", value):
            # Unlisted share classes are entered as 非上場 or a dash.
            if "取引所" in part and part not in names:
                names.append(part)
    return "、".join(names) or None


def _employees(facts: list[Fact], consolidated: bool) -> int | None:
    contexts = ("CurrentYearInstant", "CurrentYearInstant_NonConsolidatedMember")
    for context in contexts if consolidated else reversed(contexts):
        value = _int(_first(facts, "jpcrp_cor:NumberOfEmployees", context))
        if value is not None:
            return value
    return None


def _issued_shares(facts: list[Fact], years: dict[int, YearFigures]) -> int | None:
    value = _int(_first(
        facts, "jpcrp_cor:NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc",
        "FilingDateInstant"))
    if value is not None:
        return value
    latest = years.get(0)
    return _int(latest.values.get("issued_shares")) if latest else None


def _first(facts: list[Fact], element: str, context: str) -> str | None:
    for fact in facts:
        if fact.element == element and fact.context == context:
            return fact.value
    return None


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return None if value in ("", "－", "-") else value


def _number(value: str | None) -> float | None:
    text = _text(value)
    if text is None:
        return None
    text = text.replace(",", "")
    try:
        return float(text) if any(c in text for c in ".eE") else int(text)
    except ValueError:
        return None


def _int(value: str | float | None) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    number = _number(value)
    return int(number) if number is not None else None
