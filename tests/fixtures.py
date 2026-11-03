"""Small synthetic EDINET files: a code list, a daily list and a type-5 CSV ZIP."""

from __future__ import annotations

import io
import zipfile
from typing import Any

CODELIST_HEADER = (
    "ＥＤＩＮＥＴコード,提出者種別,上場区分,連結の有無,資本金,決算日,提出者名,提出者名（英字）,"
    "提出者名（ヨミ）,所在地,提出者業種,証券コード,提出者法人番号"
)


def codelist_zip(rows: list[list[str]], as_of: str = "2026年11月02日") -> bytes:
    lines = [f"ダウンロード実行日,{as_of}現在,件数,{len(rows)}件", CODELIST_HEADER]
    lines += [",".join(f'"{cell}"' for cell in row) for row in rows]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("EdinetcodeDlInfo.csv", "\r\n".join(lines).encode("cp932"))
    return buffer.getvalue()


def filer(code: str, name: str, *, listing: str = "上場", year_end: str = "3月31日",
          industry: str = "化学", sec_code: str = "") -> list[str]:
    return [code, "内国法人・組合", listing, "有", "1000", year_end, name, "X CO., LTD.",
            "エックス", "東京都千代田区", industry, sec_code or f"{code[1:5]}0", "1234567890123"]


def list_result(doc_id: str, code: str, doc_type: str = "120", **overrides: Any
                ) -> dict[str, Any]:
    result = {
        "seqNumber": 1, "docID": doc_id, "edinetCode": code, "secCode": None, "JCN": None,
        "filerName": "x", "fundCode": None, "ordinanceCode": "010", "formCode": "030000",
        "docTypeCode": doc_type, "periodStart": "2025-04-01", "periodEnd": "2026-03-31",
        "submitDateTime": "2026-06-26 15:00", "docDescription": "有価証券報告書－第1期",
        "issuerEdinetCode": None, "subjectEdinetCode": None, "subsidiaryEdinetCode": None,
        "currentReportReason": None, "parentDocID": None, "opeDateTime": None,
        "withdrawalStatus": "0", "docInfoEditStatus": "0", "disclosureStatus": "0",
        "xbrlFlag": "1", "pdfFlag": "1", "attachDocFlag": "0", "englishDocFlag": "0",
        "csvFlag": "1", "legalStatus": "1",
    }
    result.update(overrides)
    return result


def daily_list(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"metadata": {"status": "200", "message": "OK",
                         "resultset": {"count": len(results)}},
            "results": results}


CSV_HEADER = ["要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点",
              "ユニットID", "単位", "値"]


def fact(element: str, context: str, value: str, unit: str = "JPY",
         label: str | None = None) -> list[str]:
    return [element, element if label is None else label, context, "", "その他", "", unit, "",
            value]


def csv_zip(facts: list[list[str]]) -> bytes:
    lines = ["\t".join(f'"{cell}"' for cell in row) for row in [CSV_HEADER, *facts]]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL_TO_CSV/jpaud-aar-cn-001_E00001-000_2026-03-31_01_2026-06-26.csv",
                         "\r\n".join(lines[:1]).encode("utf-16"))
        archive.writestr("XBRL_TO_CSV/jpcrp030000-asr-001_E00001-000_2026-03-31_01_2026-06-26.csv",
                         "\r\n".join(lines).encode("utf-16"))
    return buffer.getvalue()


def dei(standard: str = "Japan GAAP", consolidated: str = "true") -> list[list[str]]:
    context = "FilingDateInstant"
    return [
        fact("jpdei_cor:AccountingStandardsDEI", context, standard, "－"),
        fact("jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI", context,
             consolidated, "－"),
        fact("jpdei_cor:CurrentFiscalYearStartDateDEI", context, "2025-04-01", "－"),
        fact("jpdei_cor:CurrentFiscalYearEndDateDEI", context, "2026-03-31", "－"),
        fact("jpdei_cor:PreviousFiscalYearEndDateDEI", context, "2025-03-31", "－"),
    ]


def japan_gaap_report(revenue: int = 56_474_000_000, employees: int = 1583,
                      total_assets: int = 81_039_000_000, years: int = 3) -> bytes:
    summary = "jpcrp_cor:{}SummaryOfBusinessResults"
    rows = [
        *dei(),
        fact(summary.format("NetSales"), "CurrentYearDuration", str(revenue),
             label="売上高、経営指標等"),
        *[fact(summary.format("NetSales"), f"Prior{n}YearDuration", "49815000000",
               label="売上高、経営指標等") for n in range(1, years)],
        fact(summary.format("NetSales"), "CurrentYearDuration_NonConsolidatedMember",
             "30000000000", label="売上高、経営指標等"),
        fact(summary.format("OrdinaryIncomeLoss"), "CurrentYearDuration", "6157000000",
             label="経常利益又は経常損失（△）、経営指標等"),
        fact(summary.format("ProfitLossAttributableToOwnersOfParent"), "CurrentYearDuration",
             "4995000000"),
        fact(summary.format("TotalAssets"), "CurrentYearInstant", str(total_assets)),
        fact(summary.format("EquityToAssetRatio"), "CurrentYearInstant", "0.625", "pure"),
        fact(summary.format("RateOfReturnOnEquity"), "CurrentYearDuration", "0.101", "pure"),
        fact(summary.format("RateOfReturnOnEquity"), "CurrentYearDuration_NonConsolidatedMember",
             "0.096", "pure"),
        fact(summary.format("BasicEarningsLossPerShare"), "CurrentYearDuration", "150.57",
             "JPYPerShares"),
        fact(summary.format("PriceEarningsRatio"), "CurrentYearDuration", "15.1", "pure"),
        fact(summary.format("DividendPaidPerShare"), "CurrentYearDuration_NonConsolidatedMember",
             "122.00", "JPYPerShares"),
        fact(summary.format("PayoutRatio"), "CurrentYearDuration_NonConsolidatedMember",
             "1.138", "pure"),
        fact("jppfs_cor:OperatingIncome", "CurrentYearDuration", "5805289000"),
        fact("jppfs_cor:OperatingIncome", "CurrentYearDuration_NonConsolidatedMember",
             "3338930000"),
        fact("jppfs_cor:DepreciationAndAmortizationOpeCF", "CurrentYearDuration", "2465554000"),
        fact("jpcrp_cor:NumberOfEmployees", "CurrentYearInstant", str(employees), "pure"),
        fact("jpcrp_cor:NumberOfEmployees", "CurrentYearInstant_NonConsolidatedMember", "900",
             "pure"),
        fact("jpcrp_cor:NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc",
             "FilingDateInstant", "33603924", "shares"),
        fact("jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc", "CurrentYearInstant",
             "770800", "shares"),
        fact("jpcrp_cor:NameOfFinancialInstrumentsExchangeOnWhichSecuritiesAreListedOr"
             "AuthorizedFinancialInstrumentsBusinessAssociationToWhichSecuritiesAreRegistered",
             "FilingDateInstant_OrdinaryShareMember", "東京証券取引所プライム市場", "－"),
    ]
    return csv_zip(rows)
