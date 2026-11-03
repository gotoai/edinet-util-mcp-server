"""The builder steps end to end, offline, from a pre-filled download cache."""

import json
import sqlite3
from datetime import date

import pytest

from fixtures import codelist_zip, daily_list, filer, japan_gaap_report, list_result
from snapshot import peers as peer_ranking
from snapshot import steps
from snapshot.cache import Cache
from snapshot.schema import connect

TODAY = date(2026, 11, 2)
DAY = "2026-06-26"


@pytest.fixture
def built(tmp_path) -> sqlite3.Connection:
    work = tmp_path / "work"
    (work / "codelist").mkdir(parents=True)
    (work / "codelist" / f"Edinetcode_{TODAY}.zip").write_bytes(codelist_zip([
        filer("E00001", "大きい化学", industry="化学"),
        filer("E00002", "中くらい化学", industry="化学"),
        filer("E00003", "小さい化学", industry="化学"),
        filer("E00004", "報告書なし", industry="化学"),
        filer("E00005", "十二月決算", year_end="12月31日"),
        filer("E00007", "新規上場", industry="化学"),
        filer("E00008", "外国会社", industry="化学"),
        filer("E00006", "非上場", listing="非上場"),
    ]))
    (work / "lists").mkdir()
    (work / "lists" / f"{DAY}.json").write_text(json.dumps(daily_list([
        list_result("S1", "E00001"),
        list_result("S2", "E00002"),
        list_result("S3", "E00003"),
        list_result("S3OLD", "E00003", submitDateTime="2026-06-01 09:00",
                    periodEnd="2025-03-31"),
        list_result("SW", "E00004", withdrawalStatus="1"),
        list_result("SF", "E00004", ordinanceCode="030"),
        list_result("SD", "E00005", periodEnd="2025-12-31", submitDateTime="2026-03-27 10:00"),
        list_result("SN", "E00007"),
        list_result("SC", "E00008", csvFlag="0", xbrlFlag="0"),
        list_result("SX", "E00006"),
        list_result("SH", "E00001", doc_type="160", periodEnd="2026-09-30"),
    ])))
    (work / "csv").mkdir()
    sizes = {"S1": (500e9, 5000, 600e9), "S2": (50e9, 1500, 60e9), "S3": (40e9, 1000, 50e9),
             "S3OLD": (1, 1, 1), "SD": (45e9, 1200, 55e9)}
    for doc_id, (revenue, employees, assets) in sizes.items():
        (work / "csv" / f"{doc_id}.zip").write_bytes(
            japan_gaap_report(int(revenue), employees, int(assets)))
    # Listed last year: its first annual report shows two fiscal years.
    (work / "csv" / "SN.zip").write_bytes(japan_gaap_report(41_000_000_000, 1000, int(51e9),
                                                            years=2))

    db = connect(tmp_path / "snapshot.sqlite")
    cache = Cache(work)
    messages: list[str] = []
    steps.cohort(db, cache, today=TODAY, log=messages.append)
    steps.filings(db, cache, None, start=date(2026, 6, 26), end=date(2026, 6, 26),
                  refresh=False, log=messages.append)
    steps.profiles(db, cache, None, log=messages.append)
    steps.peers(db, cache, top_n=10, log=messages.append)
    steps.finish(db, provider="GotoAI", today=TODAY, log=messages.append)
    return db


def test_cohort_is_every_listed_company(built):
    codes = [r[0] for r in built.execute("SELECT edinet_code FROM companies ORDER BY 1")]
    assert codes == ["E00001", "E00002", "E00003", "E00004", "E00005", "E00007", "E00008"]


def test_filings_skip_withdrawn_and_non_cohort(built):
    docs = {r[0] for r in built.execute("SELECT doc_id FROM filings")}
    assert docs == {"S1", "S2", "S3", "S3OLD", "SH", "SF", "SD", "SN", "SC"}


def test_profiles_use_the_latest_annual_report(built):
    # E00004's only annual report is for a trust it sponsors (ordinance 030).
    status = dict(built.execute("SELECT edinet_code, profile_status FROM companies"))
    assert status == {"E00001": "ok", "E00002": "ok", "E00003": "ok",
                      "E00004": "no_annual_report", "E00005": "ok",
                      "E00007": "short_history", "E00008": "no_csv"}
    doc_id, market, as_of = built.execute(
        "SELECT latest_annual_doc_id, market, market_as_of FROM companies"
        " WHERE edinet_code = 'E00003'").fetchone()
    assert (doc_id, market, as_of) == ("S3", "東京証券取引所プライム市場", "2026-06-26")
    revenue, employees = built.execute(
        "SELECT revenue, employees FROM financials WHERE edinet_code = 'E00003'"
        " AND period_end = '2026-03-31'").fetchone()
    assert (revenue, employees) == (40_000_000_000, 1000)


def test_peers_rank_by_scale_within_industry(built):
    ranked = built.execute(
        "SELECT peer_code, rank FROM peers WHERE edinet_code = 'E00002' ORDER BY rank").fetchall()
    assert ranked == [("E00005", 1), ("E00003", 2), ("E00001", 3)]
    reason = built.execute("SELECT reason FROM peers WHERE edinet_code = 'E00002'"
                           " AND peer_code = 'E00003'").fetchone()[0]
    assert reason.startswith("同業種（化学）・売上高0.80倍")


def test_manifest_carries_attribution(built):
    manifest = dict(built.execute("SELECT key, value FROM manifest"))
    assert manifest["snapshot_id"] == "2026-06-26"
    assert manifest["source_credit"].startswith("出典：EDINET閲覧（提出）サイト")
    assert "GotoAI が加工して作成" in manifest["processing_notice"]
    sources = {r[0] for r in built.execute("SELECT source FROM sources")}
    assert sources == {"edinet_codelist", "edinet_daily_lists", "edinet_csv"}


def test_peer_ranking_is_deterministic_on_ties():
    companies = [peer_ranking.Company(code, "化学", 100.0, 100.0, None)
                 for code in ("E3", "E1", "E2")]
    ranked = peer_ranking.rank(companies, top_n=5)
    # Same scale, no text: score is half the scale similarity, ties broken by code.
    assert [(p.code, p.peer, p.rank, p.score) for p in ranked if p.code == "E3"] == [
        ("E3", "E1", 1, 0.5), ("E3", "E2", 2, 0.5)]


def test_business_text_breaks_scale_ties():
    texts = {
        "E1": "業務用AIソリューションの受託開発と機械学習モデルの導入支援を行っております",
        "E2": "AIソリューションの受託開発と機械学習モデルの導入支援、データ分析を行っております",
        "E3": "食品スーパーマーケットを地域で運営し、生鮮食品と日用品を販売しております",
        "E4": "スーパーマーケットを運営し、生鮮食品と惣菜、日用品を販売しております",
    }
    companies = [peer_ranking.Company(code, "情報・通信業", 100.0, 100.0, 10.0, text)
                 for code, text in texts.items()]
    # Unrelated filers in another industry: they shape the vocabulary, as the whole
    # market does in a real build (max_df drops terms most documents share).
    others = ["地方銀行として預金と貸出を行っております", "鉄道とバスによる旅客輸送を行っております",
              "セメントと生コンクリートを製造しております", "医療用医薬品の研究開発を行っております",
              "損害保険と生命保険を引き受けております", "外航海運で鉄鉱石と石炭を輸送しております"]
    companies += [peer_ranking.Company(f"E9{n}", "その他", 100.0, 100.0, 10.0, text)
                  for n, text in enumerate(others)]
    ranked = [p for p in peer_ranking.rank(companies, top_n=1) if p.code < "E9"]
    assert {p.code: p.peer for p in ranked} == {"E1": "E2", "E2": "E1", "E3": "E4", "E4": "E3"}
    assert ranked[0].reason.endswith("事業内容の類似度1.00")


def test_other_industries_are_never_peers():
    companies = [peer_ranking.Company("E1", "小売業", 100.0, 100.0, 10.0, "同じ文章"),
                 peer_ranking.Company("E2", "銀行業", 100.0, 100.0, 10.0, "同じ文章")]
    assert peer_ranking.rank(companies, top_n=5) == []


def test_short_history_is_listed_without_figures_or_peers(built):
    history, market = built.execute(
        "SELECT history_years, market FROM companies WHERE edinet_code = 'E00007'").fetchone()
    assert (history, market) == (2, "東京証券取引所プライム市場")
    assert built.execute("SELECT COUNT(*) FROM financials WHERE edinet_code = 'E00007'"
                         ).fetchone()[0] == 0
    assert built.execute("SELECT COUNT(*) FROM peers WHERE 'E00007' IN (edinet_code, peer_code)"
                         ).fetchone()[0] == 0
