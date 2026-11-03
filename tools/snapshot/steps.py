"""The build steps. Each replaces only its own rows, so any step can be re-run."""

from __future__ import annotations

import sqlite3
import zipfile
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any

from edinet_util_mcp_server import api, figures
from edinet_util_mcp_server.codelist import parse_codelist

from . import peers as peer_ranking
from .cache import Cache
from .schema import FINANCIAL_COLUMNS, SCHEMA_VERSION, set_manifest, set_source

Log = Callable[[str], None]

ANNUAL_REPORT = "120"
# 企業内容等の開示に関する内閣府令: the company's own reports. A company can also file
# annual reports for trusts or funds it sponsors (ordinance 030), which are not its own.
COMPANY_ORDINANCE = "010"
# A company is covered only when its latest annual report's 5-year table has at
# least this many fiscal years, enough for trends and 3-year averages (D11).
MIN_HISTORY_YEARS = 3
HISTORY_FIELDS = ("revenue", "net_income", "total_assets")

SOURCE_CREDIT = (
    "出典：EDINET閲覧（提出）サイト（https://disclosure2.edinet-fsa.go.jp/）、"
    "PDL1.0（https://www.digital.go.jp/resources/open_data/public_data_license_v1.0）"
)


def processing_notice(provider: str) -> str:
    return f"EDINET閲覧（提出）サイトの提出書類をもとに {provider} が加工して作成"


# --- 1. cohort ---------------------------------------------------------------------------

def cohort(db: sqlite3.Connection, cache: Cache, *, today: date, log: Log) -> None:
    """Every listed company in today's code list, whatever its fiscal year end."""
    codes = parse_codelist(cache.codelist(today))
    chosen = [f for f in codes.filers if f.listed]
    with db:
        db.execute("DELETE FROM companies")
        db.executemany(
            "INSERT INTO companies (edinet_code, sec_code, jcn, name, name_en, name_kana,"
            " filer_type, industry, fiscal_year_end, capital_million_yen, address)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(f.edinet_code, f.sec_code or None, f.jcn or None, f.name, f.name_en or None,
              f.name_kana or None, f.filer_type, f.industry or None, f.fiscal_year_end,
              f.capital_million_yen, f.address or None) for f in chosen])
        set_manifest(db, cohort="listed (上場区分=上場), all fiscal year ends",
                     cohort_size=str(len(chosen)),
                     eligibility=f"latest annual report with at least {MIN_HISTORY_YEARS} "
                                 "fiscal years in its 5-year table")
        set_source(db, "edinet_codelist", api.CODELIST_URL, codes.as_of,
                   "EDINETコードリスト; PDL1.0")
    log(f"cohort: {len(chosen)} listed of {len(codes.filers)} filers in the code list "
        f"as of {codes.as_of}")


# --- 2. filings --------------------------------------------------------------------------

def filings(db: sqlite3.Connection, cache: Cache, client: api.EdinetClient | None, *,
            start: date, end: date, refresh: bool, log: Log) -> None:
    """Index every cohort filing in the window from the daily lists."""
    cohort_codes = {row[0] for row in db.execute("SELECT edinet_code FROM companies")}
    rows: dict[str, tuple[Any, ...]] = {}
    days = list(_days(start, end))
    skipped = Counter[str]()
    for i, day in enumerate(days, 1):
        if client is None and not cache.has_daily_list(day):
            raise SystemExit(f"No cached daily list for {day}, and no API key to fetch it.")
        data = cache.daily_list(client, day, refresh=refresh)  # type: ignore[arg-type]
        for result in data.get("results") or []:
            reason = _skip_reason(result, cohort_codes)
            if reason:
                skipped[reason] += 1
                continue
            rows[result["docID"]] = _filing_row(result)
        if i % 20 == 0 or i == len(days):
            log(f"filings: {i}/{len(days)} days, {len(rows)} filings "
                f"({client.requests if client else 0} API calls)")
    with db:
        db.execute("DELETE FROM filings")
        db.executemany("INSERT INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       rows.values())
        set_source(db, "edinet_daily_lists", "https://api.edinet-fsa.go.jp/api/v2/documents.json",
                   end.isoformat(),
                   f"書類一覧API, {start.isoformat()}–{end.isoformat()}; PDL1.0")
    by_type = Counter(r[2] for r in rows.values())
    log(f"filings: {len(rows)} indexed; by type {dict(by_type.most_common(8))}; "
        f"skipped {dict(skipped)}")


def _days(start: date, end: date) -> Iterable[str]:
    day = start
    while day <= end:
        yield day.isoformat()
        day += timedelta(days=1)


def _skip_reason(result: dict[str, Any], cohort_codes: set[str]) -> str | None:
    if not result.get("docID") or not result.get("docTypeCode"):
        return "no document"
    if result.get("edinetCode") not in cohort_codes:
        return "not cohort"
    if result.get("withdrawalStatus") != "0":
        return "withdrawn"
    if result.get("disclosureStatus") in ("1", "2"):
        return "not disclosed"
    if result.get("docInfoEditStatus") == "1":
        return "edit record"
    return None


def _filing_row(r: dict[str, Any]) -> tuple[Any, ...]:
    def flag(name: str) -> int:
        return 1 if r.get(name) == "1" else 0

    return (r["docID"], r["edinetCode"], r["docTypeCode"], r.get("formCode"),
            r.get("ordinanceCode"), r.get("docDescription"), r.get("periodStart"),
            r.get("periodEnd"), r.get("submitDateTime"), r.get("parentDocID"),
            flag("xbrlFlag"), flag("csvFlag"), flag("pdfFlag"), flag("englishDocFlag"),
            flag("attachDocFlag"))


# --- 3. profiles -------------------------------------------------------------------------

def profiles(db: sqlite3.Connection, cache: Cache, client: api.EdinetClient | None, *,
             log: Log) -> None:
    """Key figures and listing market from each company's latest annual report CSV."""
    latest = {code: (doc_id, bool(csv)) for code, doc_id, csv in db.execute(
        "SELECT edinet_code, doc_id, csv FROM ("
        "  SELECT edinet_code, doc_id, csv, ROW_NUMBER() OVER ("
        "    PARTITION BY edinet_code ORDER BY period_end DESC, submitted_at DESC) AS n"
        "  FROM filings WHERE doc_type_code = ? AND ordinance_code = ?)"
        " WHERE n = 1",
        (ANNUAL_REPORT, COMPANY_ORDINANCE))}
    submitted = dict(db.execute("SELECT doc_id, submitted_at FROM filings"))
    companies = [row[0] for row in db.execute("SELECT edinet_code FROM companies ORDER BY 1")]
    status = Counter[str]()
    # One transaction: downloads are cached, so a rerun after an interruption is cheap.
    with db:
        db.execute("DELETE FROM financials")
        db.execute("UPDATE companies SET market = NULL, market_as_of = NULL,"
                   " accounting_standard = NULL, latest_annual_doc_id = NULL,"
                   " history_years = NULL, profile_status = NULL")
        for i, code in enumerate(companies, 1):
            doc_id, has_csv = latest.get(code, (None, False))
            outcome = _profile(db, cache, client, code, doc_id, has_csv,
                               submitted.get(doc_id or ""), log)
            status[outcome] += 1
            if i % 50 == 0 or i == len(companies):
                log(f"profiles: {i}/{len(companies)} {dict(status)} "
                    f"({client.requests if client else 0} API calls)")
        set_source(db, "edinet_csv", "https://api.edinet-fsa.go.jp/api/v2/documents/{docID}?type=5",
                   max(submitted.values(), default=None),
                   "書類取得API type 5 (XBRL→CSV) of each company's latest annual report; "
                   "processed by the snapshot builder; PDL1.0")


def _profile(db: sqlite3.Connection, cache: Cache, client: api.EdinetClient | None,
             code: str, doc_id: str | None, has_csv: bool, submitted_at: str | None,
             log: Log) -> str:
    def mark(status: str, **fields: Any) -> str:
        columns = {"profile_status": status, "latest_annual_doc_id": doc_id, **fields}
        assignments = ", ".join(f"{name} = ?" for name in columns)
        db.execute(f"UPDATE companies SET {assignments} WHERE edinet_code = ?",
                   (*columns.values(), code))
        return status

    if doc_id is None:
        return mark("no_annual_report")
    if not has_csv:  # e.g. foreign filers, whose reports carry no XBRL
        return mark("no_csv")
    if client is None and not cache.has_csv(doc_id):
        return mark("not_fetched")
    try:
        data = cache.csv_zip(client, doc_id)  # type: ignore[arg-type]
    except api.DocumentUnavailable:
        return mark("unavailable")
    try:
        facts = figures.parse_facts(figures.report_csv_text(data))
        annual = figures.extract(facts)
    except (ValueError, KeyError, IndexError, UnicodeError, zipfile.BadZipFile) as exc:
        # One bad filing must not stop the build.
        log(f"profiles: {code} {doc_id}: {type(exc).__name__}: {exc}")
        return mark("parse_error")

    history = _history_years(annual)
    if history < MIN_HISTORY_YEARS and annual.consolidated:
        # Just started consolidating: use the full non-consolidated series rather
        # than splice one consolidated year onto non-consolidated history.
        separate = figures.extract(facts, consolidated=False)
        if _history_years(separate) >= MIN_HISTORY_YEARS:
            annual, history = separate, _history_years(separate)
    profile = {"market": annual.market,
               "market_as_of": submitted_at[:10] if submitted_at else None,
               "accounting_standard": annual.accounting_standard,
               "history_years": history}
    if history < MIN_HISTORY_YEARS:
        return mark("short_history", **profile)

    rows = []
    for year in annual.years:
        if not year.period_end:
            continue
        values = [year.values.get(name) for name in FINANCIAL_COLUMNS]
        employees = annual.employees if year.offset == 0 else None
        market_cap = annual.market_cap() if year.offset == 0 else None
        rows.append((code, year.period_end, doc_id, annual.accounting_standard, annual.basis,
                     annual.revenue_label, *values, employees, market_cap))
    columns = ("edinet_code, period_end, doc_id, standard, basis, revenue_label, "
               + ", ".join(FINANCIAL_COLUMNS) + ", employees, market_cap_fye")
    db.executemany(f"INSERT OR REPLACE INTO financials ({columns}) VALUES "
                   f"({', '.join('?' * (6 + len(FINANCIAL_COLUMNS) + 2))})", rows)
    return mark("ok", **profile)


def _history_years(annual: figures.AnnualFigures) -> int:
    return sum(1 for year in annual.years
               if year.period_end and any(year.values.get(f) is not None
                                          for f in HISTORY_FIELDS))


# --- 4. peers ----------------------------------------------------------------------------

def peers(db: sqlite3.Connection, cache: Cache, *, top_n: int, log: Log) -> None:
    """Ranked same-industry peers by scale and business text (design D9).

    The text comes from each company's latest annual-report CSV, already cached by
    the profiles step.
    """
    rows = db.execute(
        "SELECT c.edinet_code, c.industry, c.latest_annual_doc_id,"
        " f.revenue, f.total_assets, f.employees"
        " FROM companies c JOIN financials f"
        "   ON f.edinet_code = c.edinet_code AND f.doc_id = c.latest_annual_doc_id"
        " WHERE c.profile_status = 'ok' AND f.period_end = ("
        "   SELECT MAX(period_end) FROM financials g WHERE g.edinet_code = c.edinet_code)"
    ).fetchall()
    companies, without_text = [], 0
    for code, industry, doc_id, revenue, assets, employees in rows:
        text = _report_text(cache, doc_id)
        without_text += not text.strip()
        companies.append(peer_ranking.Company(code, industry, revenue, assets, employees, text))
    ranked = peer_ranking.rank(companies, top_n=top_n)
    with db:
        db.execute("DELETE FROM peers")
        db.executemany("INSERT INTO peers (edinet_code, peer_code, rank, score, reason)"
                       " VALUES (?, ?, ?, ?, ?)",
                       [(p.code, p.peer, p.rank, p.score, p.reason) for p in ranked])
        set_manifest(db, peer_algorithm=peer_ranking.ALGORITHM,
                     peer_params=f"top_n={top_n}; {peer_ranking.PARAMS}")
    with_peers = len({p.code for p in ranked})
    log(f"peers: {len(ranked)} pairs for {with_peers} of {len(companies)} companies with "
        f"figures; {without_text} without report text")


def _report_text(cache: Cache, doc_id: str) -> str:
    data = cache.cached_csv(doc_id)
    if data is None:
        return ""
    try:
        facts = figures.parse_facts(figures.report_csv_text(data))
    except (ValueError, UnicodeError, zipfile.BadZipFile):
        return ""
    sections = {f.local_name: f.value for f in facts
                if f.local_name in peer_ranking.TEXT_SECTIONS}
    return peer_ranking.report_text(sections)


# --- 5. finish ---------------------------------------------------------------------------

def finish(db: sqlite3.Connection, *, provider: str, today: date, log: Log) -> None:
    lists_as_of = db.execute(
        "SELECT as_of FROM sources WHERE source = 'edinet_daily_lists'").fetchone()
    snapshot_id = lists_as_of[0] if lists_as_of else today.isoformat()
    with db:
        set_manifest(
            db,
            schema_version=SCHEMA_VERSION,
            snapshot_id=snapshot_id,
            built_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            source_credit=SOURCE_CREDIT,
            processing_notice=processing_notice(provider),
            licence="Data: PDL1.0 (公共データ利用規約 第1.0版), processed; not the original data.",
        )
    db.execute("VACUUM")
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("companies", "filings", "financials", "peers")}
    log(f"finish: snapshot {snapshot_id}; {counts}")
