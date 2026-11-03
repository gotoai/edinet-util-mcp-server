"""The skill's query script against a small snapshot built here."""

import json
import os
import runpy
import stat
from pathlib import Path

import pytest

from snapshot.schema import connect, set_manifest

SCRIPT = (Path(__file__).resolve().parents[1]
          / "skill" / "edinet-company-profiles" / "scripts" / "edinet_profiles.py")
CREDIT = "出典：EDINET閲覧（提出）サイト（https://disclosure2.edinet-fsa.go.jp/）、PDL1.0（x）"
NOTICE = "EDINET閲覧（提出）サイトの提出書類をもとに テスト が加工して作成"

COMPANIES = [
    # edinet_code, sec_code, name, kana, industry, standard, status
    ("E00001", "55860", "株式会社Ｌａｂｏｒｏ．ＡＩ", "カブシキガイシャラボロエーアイ", "情報・通信業",
     "Japan GAAP", "ok"),
    ("E00002", "55720", "株式会社Ｒｉｄｇｅ－ｉ", "カブシキガイシャリッジアイ", "情報・通信業", "IFRS", "ok"),
    ("E00003", "55740", "株式会社ＡＢＥＪＡ", "カブシキガイシャアベジャ", "情報・通信業", "Japan GAAP", "ok"),
    ("E00004", "130A0", "株式会社ＡＩテスト", "カブシキガイシャエーアイテスト", "情報・通信業",
     "Japan GAAP", "ok"),
    ("E00005", "99990", "新設ホールディングス株式会社", "シンセツホールディングス", "情報・通信業", None,
     "no_annual_report"),
    ("E00006", "72030", "トヨタ自動車株式会社", "トヨタジドウシャ", "輸送用機器", "IFRS", "ok"),
    ("E00007", "31160", "トヨタ紡織株式会社", "トヨタボウショク", "輸送用機器", "Japan GAAP", "ok"),
]


def _years(code: str, revenue: float, margin: float, growth: float, standard: str):
    rows = []
    for n, year in enumerate(range(2022, 2027)):
        sales = round(revenue * (1 + growth) ** n)
        rows.append({
            "edinet_code": code, "period_end": f"{year}-03-31", "doc_id": f"S{code}",
            "standard": standard or "Japan GAAP", "basis": "consolidated",
            "revenue_label": "売上高", "revenue": sales,
            "operating_income": round(sales * margin * 1.4) if year >= 2025 else None,
            "net_income": round(sales * margin), "total_assets": sales * 2,
            "net_assets": sales, "equity_ratio": 0.5, "roe": margin * 2, "eps": 10.0,
            "bps": 100.0, "per": 15.0 if year == 2026 else None,
            "operating_cf": round(sales * 0.1), "investing_cf": round(-sales * 0.05),
            "cash": round(sales * 0.3), "depreciation": round(sales * 0.02) if year >= 2025
            else None, "employees": 100 if year == 2026 else None,
            "market_cap_fye": sales * 3 if year == 2026 else None,
        })
    return rows


@pytest.fixture(scope="module")
def snapshot_db(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("skill") / "data"
    path = folder / "edinet_profiles.sqlite"
    db = connect(path)
    with db:
        for code, sec, name, kana, industry, standard, status in COMPANIES:
            db.execute(
                "INSERT INTO companies (edinet_code, sec_code, name, name_kana, industry,"
                " fiscal_year_end, accounting_standard, latest_annual_doc_id, profile_status)"
                " VALUES (?, ?, ?, ?, ?, '03-31', ?, ?, ?)",
                (code, sec, name, kana, industry, standard,
                 f"S{code}" if status == "ok" else None, status))
            if status != "ok":
                continue
            db.execute("INSERT INTO filings (doc_id, edinet_code, doc_type_code, description,"
                       " period_start, period_end, submitted_at, xbrl, pdf)"
                       " VALUES (?, ?, '120', '有価証券報告書', '2025-04-01', '2026-03-31',"
                       " '2026-06-20 09:00', 1, 1)", (f"S{code}", code))
            index = int(code[-1])
            for row in _years(code, 1_000_000_000 * index, 0.05 * index, 0.1 * index, standard):
                db.execute(f"INSERT INTO financials ({', '.join(row)}) VALUES "
                           f"({', '.join('?' for _ in row)})", list(row.values()))
        ranked = {"E00001": ["E00003", "E00004", "E00002"], "E00006": ["E00007"]}
        for code, peers in ranked.items():
            for rank, peer in enumerate(peers, 1):
                db.execute("INSERT INTO peers (edinet_code, peer_code, rank, score, reason)"
                           " VALUES (?, ?, ?, ?, ?)", (code, peer, rank, 1 - rank / 10, "テスト"))
        # A new consolidator: the latest year's cash flow is not in the 5-year table.
        db.execute("UPDATE financials SET operating_cf = NULL"
                   " WHERE edinet_code = 'E00002' AND period_end = '2026-03-31'")
        set_manifest(db, snapshot_id="2026-11-02", source_credit=CREDIT,
                     processing_notice=NOTICE, peer_algorithm="test")
    db.execute("VACUUM")
    db.close()
    # Read-only, as in an imported desk folder: the script must create nothing here.
    path.chmod(stat.S_IRUSR)
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    yield path
    folder.chmod(stat.S_IRWXU)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.fixture(scope="module")
def run(snapshot_db):
    module = runpy.run_path(str(SCRIPT))

    def call(*argv: str) -> tuple[int, str]:
        return module["run"](["--db", str(snapshot_db), *argv])
    return call


@pytest.mark.parametrize("query", ["5586", "5586.T", "55860", "E00001", "laboro", "ラボロ",
                                   "らぼろ", "(株)Ｌａｂｏｒｏ．ＡＩ【5586.T】"])
def test_company_is_found_by_code_name_or_kana(run, query):
    status, text = run("company", query)
    assert status == 0, text
    assert text.startswith("# 株式会社Ｌａｂｏｒｏ．ＡＩ（5586）")


def test_alphanumeric_codes_are_shown_in_four_characters(run):
    status, text = run("company", "130A")
    assert status == 0
    assert "（130A）" in text and "130A0" not in text


def test_ambiguous_names_list_candidates(run):
    status, text = run("company", "トヨタ")
    assert status == 1
    assert "7203" in text and "3116" in text


def test_every_output_ends_with_the_attribution(run):
    for argv in (("company", "5586"), ("peers", "5586"), ("compare", "5586"),
                 ("filings", "5586"), ("sql", "SELECT 1"), ("about",), ("company", "該当なし")):
        _, text = run(*argv)
        tail = text.rsplit("---", 1)[-1]
        assert CREDIT in tail and NOTICE in tail and "2026-11-02" in tail, argv


def test_json_output_carries_the_source(run):
    status, text = run("compare", "5586", "--json")
    payload = json.loads(text)
    assert status == 0
    assert payload["source"]["source_credit"] == CREDIT
    assert payload["data"]["target"] == "E00001"


def test_compare_puts_named_peers_first_and_fills_from_candidates(run):
    status, text = run("compare", "5586", "--peers", "Ridge-i、新設ホールディングス", "-n", "3",
                       "--json")
    members = json.loads(text)["data"]["members"]
    assert status == 0
    assert [(m["edinet_code"], m["chosen_by"]) for m in members] == [
        ("E00001", "対象"), ("E00002", "指定"), ("E00003", "候補1位（0.90）"),
        ("E00004", "候補2位（0.80）")]
    excluded = json.loads(text)["data"]["excluded"]
    assert len(excluded) == 1 and "有価証券報告書の提出がない" in excluded[0]["reason"]


def test_compare_uses_the_peer_median_and_ranks_the_target(run):
    _, text = run("compare", "5586", "-n", "3", "--json")
    metrics = {m["key"]: m for m in json.loads(text)["data"]["metrics"]}
    net_margin = metrics["net_margin"]
    # Peers E00002–E00004 have net margins 0.10, 0.15, 0.20; the target 0.05.
    assert net_margin["peer_median"] == pytest.approx(0.15, abs=1e-6)
    assert (net_margin["target_rank"], net_margin["ranked_companies"]) == (4, 4)
    cagr = metrics["revenue_cagr"]["values"]["E00001"]
    assert cagr["value"] == pytest.approx(0.1, abs=1e-6) and cagr["remark"] == "4年"


def test_compare_notes_mixed_standards(run):
    _, text = run("compare", "5586", "--peers", "5572", "-n", "1")
    assert "会計基準が混在" in text
    assert "SE00002" in text  # the peer's annual report doc ID


def test_compare_refuses_a_company_without_figures(run):
    status, text = run("compare", "新設ホールディングス")
    assert status == 1
    assert "比較できません" in text


def test_sql_is_read_only_and_single_statement(run):
    assert run("sql", "DELETE FROM peers")[0] == 1
    assert run("sql", "SELECT 1; DELETE FROM peers")[0] == 1
    status, text = run("sql", "SELECT COUNT(*) AS n FROM peers")
    assert status == 0 and "| 4 |" in text


def test_the_database_folder_stays_untouched(run, snapshot_db):
    run("compare", "5586")
    assert sorted(os.listdir(snapshot_db.parent)) == ["edinet_profiles.sqlite"]


def test_a_missing_latest_value_is_never_filled_from_an_earlier_year(run):
    _, text = run("compare", "5586", "--peers", "5572", "-n", "1", "--json")
    data = json.loads(text)["data"]
    metrics = {m["key"]: m for m in data["metrics"]}
    cell = metrics["operating_cf_margin"]["values"]["E00002"]
    assert cell == {"value": None, "period_end": None, "remark": "最新期の値なし"}
    assert metrics["operating_cf_margin"]["peer_median"] is None
    assert any("Ridge-i は最新期" in note and "営業CFマージン" in note for note in data["notes"])
    _, markdown = run("compare", "5586", "--peers", "5572", "-n", "1")
    assert "—（最新期の値なし）" in markdown
