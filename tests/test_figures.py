from edinet_util_mcp_server import figures
from edinet_util_mcp_server.codelist import parse_codelist
from fixtures import codelist_zip, csv_zip, dei, fact, filer, japan_gaap_report


def extract(zip_bytes: bytes) -> figures.AnnualFigures:
    return figures.extract(figures.parse_facts(figures.report_csv_text(zip_bytes)))


def test_japan_gaap_reads_consolidated_and_company_level_lines():
    annual = extract(japan_gaap_report())
    latest = annual.latest
    assert annual.basis == "consolidated"
    assert annual.revenue_label == "売上高"
    assert latest.values["revenue"] == 56_474_000_000  # not the non-consolidated 30bn
    assert latest.values["roe"] == 0.101  # consolidated half, not 0.096
    assert latest.values["dps"] == 122.0  # only in the non-consolidated half
    assert latest.values["payout_ratio"] == 1.138
    assert latest.values["operating_income"] == 5_805_289_000  # statement, consolidated
    assert latest.values["depreciation"] == 2_465_554_000
    assert annual.employees == 1583
    assert annual.market == "東京証券取引所プライム市場"
    assert [y.period_end for y in annual.years] == ["2026-03-31", "2025-03-31", "2024-03-31"]


def test_market_cap_uses_shares_outstanding():
    annual = extract(japan_gaap_report())
    assert annual.issued_shares == 33_603_924
    assert annual.market_cap() == round(15.1 * 150.57 * (33_603_924 - 770_800))


def test_ifrs_equity_to_asset_ratio_element_is_per_share():
    summary = "jpcrp_cor:{}IFRSSummaryOfBusinessResults"
    annual = extract(csv_zip([
        *dei("IFRS"),
        fact(summary.format("Revenue"), "CurrentYearDuration", "1583719000000",
             label="売上収益（IFRS）、経営指標等"),
        fact(summary.format("EquityToAssetRatio"), "CurrentYearInstant", "804.24",
             "JPYPerShares"),
        fact(summary.format("RatioOfOwnersEquityToGrossAssets"), "CurrentYearInstant", "0.425",
             "pure"),
        fact("jpcrp030000-asr_E00436-000:TotalEquityIFRSSummaryOfBusinessResults",
             "CurrentYearInstant", "844275000000"),
        fact("jpigp_cor:OperatingProfitLossIFRS", "CurrentYearDuration", "199412000000"),
    ]))
    values = annual.latest.values
    assert annual.revenue_label == "売上収益（IFRS）"
    assert values["equity_ratio"] == 0.425
    assert values["bps"] == 804.24
    assert values["net_assets"] == 844_275_000_000  # company-specific extension element
    assert values["operating_income"] == 199_412_000_000


def test_non_consolidated_filer_reads_the_non_consolidated_half():
    summary = "jpcrp_cor:{}SummaryOfBusinessResults"
    annual = extract(csv_zip([
        *dei(consolidated="false"),
        fact(summary.format("NetSales"), "CurrentYearDuration_NonConsolidatedMember",
             "1000000000", label="売上高、経営指標等"),
        fact(summary.format("NetIncomeLoss"), "CurrentYearDuration_NonConsolidatedMember",
             "50000000"),
        fact("jppfs_cor:OperatingIncome", "CurrentYearDuration_NonConsolidatedMember",
             "80000000"),
        fact("jpcrp_cor:NumberOfEmployees", "CurrentYearInstant_NonConsolidatedMember", "120",
             "pure"),
    ]))
    assert annual.basis == "non-consolidated"
    assert annual.latest.values["revenue"] == 1_000_000_000
    assert annual.latest.values["net_income"] == 50_000_000
    assert annual.latest.values["operating_income"] == 80_000_000
    assert annual.employees == 120


def test_nil_values_and_segment_contexts_are_ignored():
    summary = "jpcrp_cor:{}SummaryOfBusinessResults"
    annual = extract(csv_zip([
        *dei(),
        fact(summary.format("NetSales"), "CurrentYearDuration", "500", label="売上高"),
        fact(summary.format("PriceEarningsRatio"), "CurrentYearDuration", "－", "pure"),
        fact(summary.format("TotalAssets"), "CurrentYearInstant_FooReportableSegmentMember",
             "999"),
    ]))
    assert annual.latest.values.get("per") is None
    assert annual.latest.values.get("total_assets") is None
    assert annual.market_cap() is None


def test_codelist_reads_month_end_and_listing():
    codes = parse_codelist(codelist_zip([
        filer("E00001", "株式会社　テスト", year_end="3月末日"),
        filer("E00002", "非上場株式会社", listing="非上場", year_end="2月末日"),
        filer("E00003", "十二月株式会社", year_end="12月31日"),
    ]))
    assert codes.as_of == "2026-11-02"
    first, second, third = codes.filers
    assert first.name == "株式会社 テスト"
    assert first.fiscal_year_end == "03-31" and first.listed
    assert second.fiscal_year_end == "02-28" and not second.listed
    assert third.fiscal_year_end_month == 12


def test_filer_that_stopped_consolidating_uses_the_non_consolidated_half():
    summary = "jpcrp_cor:{}SummaryOfBusinessResults"
    annual = extract(csv_zip([
        *dei(consolidated="false"),
        fact(summary.format("NetSales"), "Prior1YearDuration", "4422391000", label="売上高"),
        fact(summary.format("NetSales"), "CurrentYearDuration", "－", label="売上高"),
        fact(summary.format("NetSales"), "Prior1YearDuration_NonConsolidatedMember",
             "2492948000", label="売上高"),
        fact(summary.format("NetSales"), "CurrentYearDuration_NonConsolidatedMember",
             "2399669000", label="売上高"),
    ]))
    assert annual.basis == "non-consolidated"
    assert [y.values["revenue"] for y in annual.years] == [2_399_669_000, 2_492_948_000]


def test_revenue_element_with_a_blank_label():
    annual = extract(csv_zip([
        *dei(),
        fact("jpcrp_cor:NetSalesOfCompletedConstructionContractsSummaryOfBusinessResults",
             "CurrentYearDuration", "700000000", label=""),
    ]))
    assert annual.revenue_label == "完成工事高"
    assert annual.latest.values["revenue"] == 700_000_000


def test_ifrs_revenue_from_the_statement_when_the_summary_has_none():
    annual = extract(csv_zip([
        *dei("IFRS"),
        fact("jpcrp_cor:ProfitLossBeforeTaxIFRSSummaryOfBusinessResults", "CurrentYearDuration",
             "90", label="税引前利益（IFRS）"),
        fact("jpigp_cor:RevenueIFRS", "CurrentYearDuration", "1000"),
        fact("jpigp_cor:RevenueIFRS", "Prior1YearDuration", "900"),
    ]))
    assert annual.revenue_label == "売上収益（IFRS）"
    assert [y.values["revenue"] for y in annual.years] == [1000, 900]


def test_market_names_are_normalised():
    element = ("jpcrp_cor:NameOfFinancialInstrumentsExchangeOnWhichSecuritiesAreListedOr"
               "AuthorizedFinancialInstrumentsBusinessAssociationToWhichSecuritiesAreRegistered")
    for raw, expected in [
        ("東京証券取引所プライム市場名古屋証券取引所プレミア市場",
         "東京証券取引所プライム市場、名古屋証券取引所プレミア市場"),
        ("東京証券取引所（プライム市場）", "東京証券取引所プライム市場"),
        ("株式会社東京証券取引所 スタンダード市場", "東京証券取引所スタンダード市場"),
    ]:
        annual = extract(csv_zip([*dei(), fact(element, "FilingDateInstant_OrdinaryShareMember",
                                               raw, "－")]))
        assert annual.market == expected


def test_key_financial_data_elements_count_as_the_five_year_table():
    annual = extract(csv_zip([
        *dei("IFRS"),
        fact("jpcrp030000-asr_E02144-000:OperatingRevenuesIFRSKeyFinancialData",
             "CurrentYearDuration", "48000000000000", label=""),
        fact("jpcrp_cor:RevenueKeyFinancialData", "CurrentYearDuration_NonConsolidatedMember",
             "1", label="売上収益、経営指標等"),
    ]))
    assert annual.revenue_label == "営業収益"
    assert annual.latest.values["revenue"] == 48_000_000_000_000


def test_unlisted_share_classes_are_not_markets():
    element = ("jpcrp_cor:NameOfFinancialInstrumentsExchangeOnWhichSecuritiesAreListedOr"
               "AuthorizedFinancialInstrumentsBusinessAssociationToWhichSecuritiesAreRegistered")
    annual = extract(csv_zip([
        *dei(),
        fact(element, "FilingDateInstant_OrdinaryShareMember", "東京証券取引所プライム市場", "－"),
        fact(element, "FilingDateInstant_ClassAPreferredSharesMember", "非上場", "－"),
    ]))
    assert annual.market == "東京証券取引所プライム市場"


def test_basis_can_be_overridden_for_a_new_consolidator():
    summary = "jpcrp_cor:{}SummaryOfBusinessResults"
    facts = figures.parse_facts(figures.report_csv_text(csv_zip([
        *dei(consolidated="true"),
        fact(summary.format("NetSales"), "CurrentYearDuration", "1900", label="売上高"),
        *[fact(summary.format("NetSales"), f"{p}Duration_NonConsolidatedMember", v, label="売上高")
          for p, v in (("CurrentYear", "1892"), ("Prior1Year", "1515"), ("Prior2Year", "1369"))],
    ])))
    assert len(figures.extract(facts).years) == 1
    separate = figures.extract(facts, consolidated=False)
    assert separate.basis == "non-consolidated"
    assert [y.values["revenue"] for y in separate.years] == [1892, 1515, 1369]
