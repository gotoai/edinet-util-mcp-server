#!/usr/bin/env python3
"""Query the EDINET company-profile snapshot. Standard library only; read-only.

    python scripts/edinet_profiles.py company <query>
    python scripts/edinet_profiles.py peers <query> [-n 10]
    python scripts/edinet_profiles.py compare <query> [--peers "A,B"] [-n 5]
    python scripts/edinet_profiles.py filings <query> [--types 120,160] [--limit 30]
    python scripts/edinet_profiles.py sql "SELECT ..."
    python scripts/edinet_profiles.py about

<query> is a securities code (5586, 5586.T, 55860), an EDINET code (E38755) or part
of the company name (漢字, カナ or English). Output is Markdown; add --json for
structured output. Every output ends with the source credit, the processing notice
and the snapshot's as-of date.

The database is opened in place with mode=ro&immutable=1, so its folder may be
read-only and no journal file is ever created next to it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "edinet_profiles.sqlite"
DEFAULT_PEERS = 5
MAX_PEERS = 10
DEFAULT_SQL_ROWS = 200

STATUS_NOTES = {
    "ok": "",
    "short_history": "最新の有価証券報告書の5年推移が3期未満のため、指標と同業候補はありません。",
    "no_annual_report": "収録期間に有価証券報告書の提出がないため、指標と同業候補はありません"
                        "（上場直後・持株会社化直後など）。",
    "no_csv": "有価証券報告書にXBRL（CSV）がないため、指標と同業候補はありません（外国会社など）。",
    "unavailable": "有価証券報告書のCSVをEDINETから取得できなかったため、指標と同業候補はありません。",
    "parse_error": "有価証券報告書の読み取りに失敗したため、指標と同業候補はありません。",
}

DOC_TYPES = {
    "010": "有価証券通知書", "020": "変更通知書（有価証券通知書）", "030": "有価証券届出書",
    "040": "訂正有価証券届出書", "050": "届出の取下げ願い", "060": "発行登録通知書",
    "070": "変更通知書（発行登録通知書）", "080": "発行登録書", "090": "訂正発行登録書",
    "100": "発行登録追補書類", "110": "発行登録取下届出書", "120": "有価証券報告書",
    "130": "訂正有価証券報告書", "135": "確認書", "136": "訂正確認書", "140": "四半期報告書",
    "150": "訂正四半期報告書", "160": "半期報告書", "170": "訂正半期報告書", "180": "臨時報告書",
    "190": "訂正臨時報告書", "200": "親会社等状況報告書", "210": "訂正親会社等状況報告書",
    "220": "自己株券買付状況報告書", "230": "訂正自己株券買付状況報告書", "235": "内部統制報告書",
    "236": "訂正内部統制報告書", "240": "公開買付届出書", "250": "訂正公開買付届出書",
    "260": "公開買付撤回届出書", "270": "公開買付報告書", "280": "訂正公開買付報告書",
    "290": "意見表明報告書", "300": "訂正意見表明報告書", "310": "対質問回答報告書",
    "320": "訂正対質問回答報告書", "350": "大量保有報告書", "360": "訂正大量保有報告書",
    "370": "基準日の届出書", "380": "変更の届出書",
}

BASIS = {"consolidated": "連結", "non-consolidated": "単体"}
STANDARDS = {"Japan GAAP": "日本基準", "IFRS": "IFRS", "US GAAP": "米国基準", "JMIS": "JMIS"}

_EDINET_CODE = re.compile(r"E\d{5}")
_SEC_CODE = re.compile(r"([0-9][0-9A-Z]{3})[0-9]?(?:\.T)?")
_BRACKETED_CODE = re.compile(r"[【\[(]\s*([0-9][0-9A-Z]{3})[0-9]?(?:\.T)?\s*[】\])]")
_CORPORATE = re.compile(r"株式会社|\(株\)|有限会社|合同会社|カブシキガイシャ|ユウゲンガイシャ")
_IGNORED = re.compile(r"[\s・.,'’\-&＆]")
_HIRAGANA = {code: code + 0x60 for code in range(0x3041, 0x3097)}


class QueryError(Exception):
    """A message for the person or agent, not a crash."""


# ---------------------------------------------------------------- database ----


def open_db(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise QueryError(f"データベースが見つかりません: {path}")
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def manifest(db: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in db.execute("SELECT key, value FROM manifest")}


def companies(db: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute("SELECT * FROM companies ORDER BY edinet_code")]


def financials(db: sqlite3.Connection, edinet_code: str) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(
        "SELECT * FROM financials WHERE edinet_code = ? ORDER BY period_end", (edinet_code,))]


def peer_rows(db: sqlite3.Connection, edinet_code: str, limit: int) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(
        "SELECT p.rank, p.score, p.reason, c.* FROM peers p"
        " JOIN companies c ON c.edinet_code = p.peer_code"
        " WHERE p.edinet_code = ? ORDER BY p.rank LIMIT ?", (edinet_code, limit))]


def latest_annual(db: sqlite3.Connection, company: dict[str, Any]) -> dict[str, Any] | None:
    doc_id = company.get("latest_annual_doc_id")
    if not doc_id:
        return None
    row = db.execute("SELECT * FROM filings WHERE doc_id = ?", (doc_id,)).fetchone()
    return dict(row) if row else {"doc_id": doc_id}


# ---------------------------------------------------------------- lookup ------


def _name_key(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _CORPORATE.sub("", text).translate(_HIRAGANA)
    return _IGNORED.sub("", text).lower()


def _query_code(query: str) -> str | None:
    match = _SEC_CODE.fullmatch(query)
    if match:
        return match.group(1)
    match = _BRACKETED_CODE.search(query)
    return match.group(1) if match else None


def resolve(all_companies: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """The one listed company a query names; QueryError listing candidates otherwise."""
    text = unicodedata.normalize("NFKC", query).strip()
    upper = text.upper()
    if _EDINET_CODE.fullmatch(upper):
        hits = [c for c in all_companies if c["edinet_code"] == upper]
    elif code := _query_code(upper):
        hits = [c for c in all_companies if (c["sec_code"] or "")[:4] == code]
    else:
        key = _name_key(text)
        if not key:
            raise QueryError("企業名か証券コードを指定してください。")
        names = [(c, (_name_key(c["name"]), _name_key(c["name_en"]), _name_key(c["name_kana"])))
                 for c in all_companies]
        hits = [c for c, keys in names if key in keys]
        if not hits:
            hits = [c for c, keys in names if any(key in k for k in keys if k)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise QueryError(f"「{query}」に当たる上場企業がありません。収録は上場企業だけです。"
                         "表記を変えるか、証券コード・EDINETコードで指定してください。")
    lines = [(f"「{query}」に当たる企業が {len(hits)} 社あります。証券コードかEDINETコードで"
              "指定してください。"), ""]
    for c in sorted(hits, key=lambda c: (c["profile_status"] != "ok", len(c["name"])))[:20]:
        lines.append(f"- {code4(c)}　{c['name']}（{c['edinet_code']}、{c['industry'] or '—'}）")
    if len(hits) > 20:
        lines.append(f"- ほか {len(hits) - 20} 社")
    raise QueryError("\n".join(lines))


def code4(company: dict[str, Any]) -> str:
    """The securities code as quoted (5586), not EDINET's 5-character form (55860)."""
    sec = company.get("sec_code") or ""
    return sec[:4] if len(sec) == 5 else (sec or "—")


def short_name(company: dict[str, Any]) -> str:
    name = unicodedata.normalize("NFKC", company["name"])
    return re.sub(r"\s+", " ", _CORPORATE.sub("", name)).strip() or name


def label(company: dict[str, Any]) -> str:
    return f"{short_name(company)}（{code4(company)}）"


# ---------------------------------------------------------------- formatting --


def fmt_million(value: float | None) -> str:
    if value is None:
        return "—"
    million = value / 1_000_000
    return f"{million:,.1f}" if abs(million) < 10 else f"{million:,.0f}"


def fmt_pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def fmt_times(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}倍"


def fmt_number(value: float | None, digits: int = 0) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def fiscal_label(period_end: str | None) -> str:
    return f"{period_end[2:4]}/{period_end[5:7]}期" if period_end else "—"


def table(header: list[str], rows: list[list[str]]) -> str:
    def cell(text: Any) -> str:
        return str(text).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(cell(h) for h in header) + " |",
             "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def attribution(meta: dict[str, str], db: sqlite3.Connection) -> dict[str, str]:
    window = db.execute("SELECT MIN(submitted_at), MAX(submitted_at) FROM filings").fetchone()
    return {
        "source_credit": meta.get("source_credit", ""),
        "processing_notice": meta.get("processing_notice", ""),
        "snapshot_id": meta.get("snapshot_id", ""),
        "filings_window": f"{(window[0] or '')[:10]}〜{(window[1] or '')[:10]}",
    }


def footer(source: dict[str, str]) -> str:
    return "\n".join([
        "---",
        source["source_credit"],
        source["processing_notice"],
        (f"データ時点：スナップショット {source['snapshot_id']}"
         f"（収録した提出書類 {source['filings_window']}）。加工データであり、原本ではありません。"),
    ])


# ---------------------------------------------------------------- metrics -----


Row = dict[str, Any]


def _div(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b not in (None, 0) else None


def _sum(*values: float | None) -> float | None:
    return None if any(v is None for v in values) else sum(values)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    unit: str  # pct | million | times | yen | count
    value: Callable[[Row, Row | None], float | None] | None = None
    series: Callable[[list[Row]], tuple[float | None, str | None, str]] | None = None
    note: str = ""

    def compute(self, rows: list[Row]) -> tuple[float | None, str | None, str]:
        """The value, the fiscal year end it is for, and a remark for the cell."""
        if self.series:
            return self.series(rows)
        assert self.value is not None
        for index in range(len(rows) - 1, -1, -1):
            previous = rows[index - 1] if index else None
            value = self.value(rows[index], previous)
            if value is not None and math.isfinite(value):
                return value, rows[index]["period_end"], ""
        return None, None, ""


def _cagr(rows: list[Row]) -> tuple[float | None, str | None, str]:
    points = [r for r in rows if (r["revenue"] or 0) > 0]
    if len(points) < 2:
        return None, None, ""
    first, last = points[0], points[-1]
    years = round((date.fromisoformat(last["period_end"])
                   - date.fromisoformat(first["period_end"])).days / 365.25)
    if years < 2:
        return None, None, ""
    return (last["revenue"] / first["revenue"]) ** (1 / years) - 1, last["period_end"], f"{years}年"


def _average(per_row: Callable[[Row], float | None]
             ) -> Callable[[list[Row]], tuple[float | None, str | None, str]]:
    def compute(rows: list[Row]) -> tuple[float | None, str | None, str]:
        values = [(v, r["period_end"]) for r in rows[-3:] if (v := per_row(r)) is not None]
        if len(values) < 2:
            return None, None, ""
        return statistics.fmean(v for v, _ in values), values[-1][1], f"{len(values)}期平均"
    return compute


def _growth(r: Row, p: Row | None) -> float | None:
    ratio = _div(r["revenue"], p["revenue"]) if p else None
    return ratio - 1 if ratio is not None and ratio > 0 else None


def _net_margin(r: Row) -> float | None:
    return _div(r["net_income"], r["revenue"])


def _price(r: Row) -> float | None:
    return r["per"] * r["eps"] if r["per"] is not None and r["eps"] is not None else None


METRICS: tuple[Metric, ...] = (
    Metric("revenue", "売上高（百万円）", "million", lambda r, p: r["revenue"]),
    Metric("revenue_growth", "売上高成長率（前期比）", "pct", lambda r, p: _growth(r, p)),
    Metric("revenue_cagr", "売上高CAGR", "pct", series=_cagr),
    Metric("operating_margin", "営業利益率", "pct",
           lambda r, p: _div(r["operating_income"], r["revenue"]),
           note="会計基準で営業利益の定義が異なる"),
    Metric("ebitda_margin", "EBITDAマージン", "pct",
           lambda r, p: _div(_sum(r["operating_income"], r["depreciation"]), r["revenue"]),
           note="営業利益＋減価償却費"),
    Metric("net_margin", "当期純利益率", "pct", lambda r, p: _net_margin(r)),
    Metric("net_margin_3y", "当期純利益率（3期平均）", "pct", series=_average(_net_margin)),
    Metric("roe", "ROE", "pct", lambda r, p: r["roe"]),
    Metric("roe_3y", "ROE（3期平均）", "pct", series=_average(lambda r: r["roe"])),
    Metric("asset_turnover", "総資産回転率", "times",
           lambda r, p: _div(r["revenue"], r["total_assets"]), note="売上高÷総資産"),
    Metric("leverage", "財務レバレッジ", "times",
           lambda r, p: _div(r["total_assets"], r["net_assets"])
           if (r["net_assets"] or 0) > 0 else None, note="総資産÷純資産"),
    Metric("equity_ratio", "自己資本比率", "pct", lambda r, p: r["equity_ratio"]),
    Metric("cash_to_assets", "現金同等物÷総資産", "pct",
           lambda r, p: _div(r["cash"], r["total_assets"])),
    Metric("operating_cf_margin", "営業CFマージン", "pct",
           lambda r, p: _div(r["operating_cf"], r["revenue"])),
    Metric("fcf_margin", "FCFマージン", "pct",
           lambda r, p: _div(_sum(r["operating_cf"], r["investing_cf"]), r["revenue"]),
           note="（営業CF＋投資CF）÷売上高"),
    Metric("payout_ratio", "配当性向", "pct", lambda r, p: r["payout_ratio"],
           note="連結の記載がなければ提出会社単体"),
    Metric("dps", "1株配当（円）", "yen", lambda r, p: r["dps"]),
    Metric("per", "PER（期末）", "times", lambda r, p: r["per"]),
    Metric("pbr", "PBR（期末）", "times",
           lambda r, p: _div(_price(r), r["bps"]) if (r["bps"] or 0) > 0 else None,
           note="PER×EPS÷BPS"),
    Metric("market_cap", "時価総額（期末、百万円）", "million", lambda r, p: r["market_cap_fye"]),
    Metric("employees", "従業員数（人）", "count", lambda r, p: r["employees"]),
    Metric("revenue_per_employee", "従業員1人当たり売上高（百万円）", "million",
           lambda r, p: _div(r["revenue"], r["employees"])),
)


def fmt_metric(unit: str, value: float | None) -> str:
    if unit == "pct":
        return fmt_pct(value)
    if unit == "million":
        return fmt_million(value)
    if unit == "times":
        return fmt_times(value)
    if unit == "yen":
        return fmt_number(value, 1)
    return fmt_number(value)


def fmt_difference(unit: str, target: float | None, median: float | None) -> str:
    if target is None or median is None:
        return "—"
    if unit == "pct":
        return f"{(target - median) * 100:+.1f}pt"
    if unit in ("million", "count", "yen"):
        return f"{target / median:.2f}倍" if median > 0 else "—"
    return f"{target - median:+.2f}"


# ---------------------------------------------------------------- commands ----


@dataclass
class Context:
    db: sqlite3.Connection
    meta: dict[str, str]
    source: dict[str, str]
    companies: list[dict[str, Any]]


def cmd_company(ctx: Context, args: argparse.Namespace) -> tuple[str, Any]:
    company = resolve(ctx.companies, args.query)
    rows = financials(ctx.db, company["edinet_code"])
    annual = latest_annual(ctx.db, company)
    peers = peer_rows(ctx.db, company["edinet_code"], 3)
    basis = BASIS.get(rows[-1]["basis"], "—") if rows else "—"
    profile = [
        ["証券コード", code4(company)],
        ["EDINETコード", company["edinet_code"]],
        ["法人番号", company["jcn"] or "—"],
        ["英文名", company["name_en"] or "—"],
        ["業種（東証33業種）", company["industry"] or "—"],
        ["上場市場", f"{company['market'] or '—'}（{company['market_as_of'] or '—'} 時点）"],
        ["決算日", _fiscal_year_end(company["fiscal_year_end"])],
        ["会計基準", STANDARDS.get(company["accounting_standard"] or "", "—")],
        ["数値の範囲", basis],
        ["資本金", f"{fmt_number(company['capital_million_yen'])} 百万円"],
        ["所在地", company["address"] or "—"],
        ["最新の有価証券報告書", _doc_line(annual)],
    ]
    note = STATUS_NOTES.get(company["profile_status"] or "", company["profile_status"] or "")
    parts = [f"# {company['name']}（{code4(company)}）", "", table(["項目", "内容"], profile)]
    if note:
        parts += ["", f"**注意：** {note}"]
    if rows:
        parts += ["", "## 5年の推移（金額は百万円）", "", _history_table(rows)]
    if peers:
        parts += ["", "同業候補の上位：" + "、".join(label(p) for p in peers)
                  + "（詳しくは peers コマンド）"]
    data = {"company": company, "financials": rows, "latest_annual_report": annual,
            "status_note": note, "top_peers": [p["edinet_code"] for p in peers]}
    return "\n".join(parts), data


def _fiscal_year_end(value: str | None) -> str:
    if not value or "-" not in value:
        return "—"
    month, day = value.split("-")
    return f"{int(month)}月{int(day)}日"


def _doc_line(annual: dict[str, Any] | None) -> str:
    if not annual:
        return "—"
    submitted = (annual.get("submitted_at") or "")[:10]
    return f"{annual['doc_id']}（{submitted} 提出）" if submitted else annual["doc_id"]


HISTORY_ROWS: tuple[tuple[str, str, str], ...] = (
    ("revenue", "売上高", "million"), ("operating_income", "営業利益", "million"),
    ("ordinary_income", "経常利益", "million"), ("net_income", "当期純利益", "million"),
    ("comprehensive_income", "包括利益", "million"), ("total_assets", "総資産", "million"),
    ("net_assets", "純資産", "million"), ("equity_ratio", "自己資本比率", "pct"),
    ("roe", "ROE", "pct"), ("eps", "EPS（円）", "yen"), ("bps", "BPS（円）", "yen"),
    ("per", "PER", "times"), ("dps", "1株配当（円）", "yen"),
    ("payout_ratio", "配当性向（連結の記載がなければ単体）", "pct"),
    ("operating_cf", "営業CF", "million"), ("investing_cf", "投資CF", "million"),
    ("financing_cf", "財務CF", "million"), ("cash", "現金同等物", "million"),
    ("depreciation", "減価償却費", "million"), ("employees", "従業員数（人）", "count"),
    ("market_cap_fye", "時価総額（期末）", "million"),
)


def _history_table(rows: list[Row]) -> str:
    header = ["項目"] + [fiscal_label(r["period_end"]) for r in rows]
    revenue_label = rows[-1]["revenue_label"] or "売上高"
    body = []
    for key, name, unit in HISTORY_ROWS:
        values = [r[key] for r in rows]
        if all(v is None for v in values):
            continue
        shown = revenue_label if key == "revenue" else name
        body.append([shown] + [fmt_metric(unit, v) for v in values])
    return table(header, body)


def cmd_peers(ctx: Context, args: argparse.Namespace) -> tuple[str, Any]:
    company = resolve(ctx.companies, args.query)
    peers = peer_rows(ctx.db, company["edinet_code"], args.n)
    title = f"# {label(company)} の同業候補"
    if not peers:
        note = STATUS_NOTES.get(company["profile_status"] or "") or "同業候補がありません。"
        return f"{title}\n\n{note}", {"company": company, "peers": []}
    body = [[str(p["rank"]), code4(p), p["name"], f"{p['score']:.2f}", p["reason"]]
            for p in peers]
    method = ctx.meta.get("peer_algorithm", "")
    text = "\n".join([
        title, "",
        table(["順位", "証券コード", "社名", "スコア", "理由"], body), "",
        ("同じ東証33業種の中で、規模（売上高・総資産・従業員数）の近さと、有価証券報告書の"
         "「事業の内容」「経営方針」の文章の近さを半々で採点した候補です（"
         f"{method}）。競合かどうかは事業内容を読んで確かめてください。"),
    ])
    return text, {"company": company, "peers": peers, "algorithm": method}


@dataclass
class Member:
    company: dict[str, Any]
    role: str  # target | peer
    chosen_by: str
    rows: list[Row]


def choose_peers(ctx: Context, target: dict[str, Any], requested: list[str], size: int
                 ) -> tuple[list[Member], list[tuple[str, str]]]:
    """User-named peers first, then the snapshot's candidates in rank order."""
    chosen: list[Member] = []
    excluded: list[tuple[str, str]] = []
    seen = {target["edinet_code"]}
    for query in requested:
        try:
            company = resolve(ctx.companies, query)
        except QueryError as error:
            excluded.append((query, str(error).splitlines()[0]))
            continue
        if company["edinet_code"] in seen:
            excluded.append((query, "対象企業か、すでに選んだ企業と同じです。"))
            continue
        seen.add(company["edinet_code"])
        rows = financials(ctx.db, company["edinet_code"])
        if not rows:
            excluded.append((label(company), STATUS_NOTES.get(company["profile_status"] or "")
                             or "財務データがありません。"))
            continue
        chosen.append(Member(company, "peer", "指定", rows))
    for candidate in peer_rows(ctx.db, target["edinet_code"], MAX_PEERS):
        if len(chosen) >= max(size, len(requested)):
            break
        if candidate["edinet_code"] in seen:
            continue
        seen.add(candidate["edinet_code"])
        rows = financials(ctx.db, candidate["edinet_code"])
        if rows:
            chosen.append(Member(candidate, "peer",
                                 f"候補{candidate['rank']}位（{candidate['score']:.2f}）", rows))
    return chosen, excluded


def cmd_compare(ctx: Context, args: argparse.Namespace) -> tuple[str, Any]:
    target = resolve(ctx.companies, args.query)
    target_rows = financials(ctx.db, target["edinet_code"])
    if not target_rows:
        note = STATUS_NOTES.get(target["profile_status"] or "") or "財務データがありません。"
        raise QueryError(f"{label(target)} は比較できません。{note}")
    requested = [q.strip() for item in (args.peers or []) for q in re.split(r"[,、;；]", item)
                 if q.strip()]
    peers, excluded = choose_peers(ctx, target, requested, args.n)
    members = [Member(target, "target", "対象", target_rows), *peers]

    results = []
    for metric in METRICS:
        values = {m.company["edinet_code"]: metric.compute(m.rows) for m in members}
        peer_values = [values[m.company["edinet_code"]][0] for m in peers
                       if values[m.company["edinet_code"]][0] is not None]
        target_value = values[target["edinet_code"]][0]
        ranked = sorted((v[0] for v in values.values() if v[0] is not None), reverse=True)
        results.append({
            "metric": metric,
            "values": values,
            "median": statistics.median(peer_values) if peer_values else None,
            "min": min(peer_values) if peer_values else None,
            "max": max(peer_values) if peer_values else None,
            "rank": ranked.index(target_value) + 1 if target_value is not None else None,
            "ranked": len(ranked),
        })
    notes = _compare_notes(members)
    text = _compare_markdown(ctx, members, excluded, results, notes)
    data = {
        "target": target["edinet_code"],
        "members": [{"edinet_code": m.company["edinet_code"], "sec_code": code4(m.company),
                     "name": m.company["name"], "role": m.role, "chosen_by": m.chosen_by,
                     "industry": m.company["industry"], "standard": m.rows[-1]["standard"],
                     "basis": m.rows[-1]["basis"], "latest_period_end": m.rows[-1]["period_end"],
                     "annual_report_doc_id": m.company["latest_annual_doc_id"]}
                    for m in members],
        "excluded": [{"query": q, "reason": r} for q, r in excluded],
        "metrics": [{"key": r["metric"].key, "label": r["metric"].label,
                     "unit": r["metric"].unit, "note": r["metric"].note,
                     "values": {code: {"value": v, "period_end": pe, "remark": rm}
                                for code, (v, pe, rm) in r["values"].items()},
                     "peer_median": r["median"], "peer_min": r["min"], "peer_max": r["max"],
                     "target_rank": r["rank"], "ranked_companies": r["ranked"]}
                    for r in results],
        "series": {m.company["edinet_code"]: m.rows for m in members},
        "notes": notes,
    }
    return text, data


def _cell(unit: str, value: tuple[float | None, str | None, str], latest: str) -> str:
    number, period_end, remark = value
    text = fmt_metric(unit, number)
    extra = [remark] if remark else []
    if number is not None and period_end and period_end != latest:
        extra.append(fiscal_label(period_end))
    return f"{text}（{'、'.join(extra)}）" if extra else text


def _compare_markdown(ctx: Context, members: list[Member], excluded: list[tuple[str, str]],
                      results: list[dict[str, Any]], notes: list[str]) -> str:
    target, peers = members[0], members[1:]
    lines = [f"# {label(target.company)} と同業他社の比較", ""]
    lines += ["## 比較する企業", "", table(
        ["区分", "証券コード", "社名", "業種", "会計基準", "範囲", "最新期", "選び方"],
        [["対象" if m.role == "target" else "同業", code4(m.company), m.company["name"],
          m.company["industry"] or "—", STANDARDS.get(m.rows[-1]["standard"] or "", "—"),
          BASIS.get(m.rows[-1]["basis"], "—"), fiscal_label(m.rows[-1]["period_end"]),
          m.chosen_by] for m in members])]
    if excluded:
        lines += ["", "比較から外した企業："]
        lines += [f"- {query}：{reason}" for query, reason in excluded]
    if not peers:
        lines += ["", "比較できる同業他社がありません。"]

    header = ["指標", label(target.company), "同業中央値", "差", "順位（大きい順）"]
    header += [label(m.company) for m in peers]
    body = []
    for r in results:
        metric = r["metric"]
        if all(v[0] is None for v in r["values"].values()):
            continue
        row = [metric.label + (f"※{metric.note}" if metric.note else ""),
               _cell(metric.unit, r["values"][target.company["edinet_code"]],
                     target.rows[-1]["period_end"]),
               fmt_metric(metric.unit, r["median"]),
               fmt_difference(metric.unit, r["values"][target.company["edinet_code"]][0],
                              r["median"]),
               f"{r['rank']}/{r['ranked']}" if r["rank"] else "—"]
        row += [_cell(metric.unit, r["values"][m.company["edinet_code"]],
                      m.rows[-1]["period_end"]) for m in peers]
        body.append(row)
    lines += ["", "## 指標（各社の最新期。括弧内は使った期・期間）", "", table(header, body)]

    lines += ["", "## 売上高の推移（百万円。当期＝各社の最新期）", "",
              _series_table(members, lambda r, p: r["revenue"], "million")]
    lines += ["", "## 売上高成長率（前期比）", "",
              _series_table(members, _growth, "pct", median=True)]
    lines += ["", "## 当期純利益率", "",
              _series_table(members, lambda r, p: _net_margin(r), "pct", median=True)]

    checks = _market_checks(members, results)
    if checks:
        lines += ["", "## 日本市場の目安との比較", ""] + checks
    lines += ["", "## 有価証券報告書（本文は edinet-util の export_filing で取得）", "",
              table(["社名", "書類管理番号", "提出日", "対象期間"],
                    [_report_row(ctx, m) for m in members])]
    lines += ["", "## 注記", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines)


def _series_table(members: list[Member], value: Callable[[Row, Row | None], float | None],
                  unit: str, median: bool = False) -> str:
    width = max(len(m.rows) for m in members)
    names = ["当期" if i == 0 else f"{i}期前" for i in range(width)][::-1]
    body, columns = [], [[] for _ in range(width)]
    for m in members:
        cells = []
        for offset in range(width):
            index = len(m.rows) - width + offset
            v = None
            if index >= 0:
                v = value(m.rows[index], m.rows[index - 1] if index else None)
            if m.role == "peer" and v is not None:
                columns[offset].append(v)
            cells.append(fmt_metric(unit, v))
        body.append([label(m.company), fiscal_label(m.rows[-1]["period_end"])] + cells)
    if median and len(members) > 1:
        body.append(["同業中央値", ""] + [fmt_metric(unit, statistics.median(c)) if c else "—"
                                          for c in columns])
    return table(["社名", "決算期"] + names, body)


def _market_checks(members: list[Member], results: list[dict[str, Any]]) -> list[str]:
    by_key = {r["metric"].key: r for r in results}
    lines = []
    for m in members:
        code = m.company["edinet_code"]
        flags = []
        pbr = by_key["pbr"]["values"][code][0]
        roe = by_key["roe"]["values"][code][0]
        if pbr is not None and pbr < 1:
            flags.append(f"PBR {pbr:.2f}倍（1倍割れ：東証の要請の対象）")
        if roe is not None and roe < 0.08:
            flags.append(f"ROE {roe * 100:.1f}%（8%未満：伊藤レポートの目安を下回る）")
        if flags:
            lines.append(f"- {label(m.company)}：" + "、".join(flags))
    return lines


def _report_row(ctx: Context, m: Member) -> list[str]:
    annual = latest_annual(ctx.db, m.company) or {}
    period = "〜".join(p for p in (annual.get("period_start"), annual.get("period_end")) if p)
    return [label(m.company), annual.get("doc_id") or "—",
            (annual.get("submitted_at") or "")[:10] or "—", period or "—"]


def _compare_notes(members: list[Member]) -> list[str]:
    notes = []
    ends = {m.rows[-1]["period_end"] for m in members}
    if len(ends) > 1:
        notes.append("各社の最新期末が異なります（" + "、".join(
            f"{short_name(m.company)} {m.rows[-1]['period_end']}" for m in members)
            + "）。比較は各社の最新期どうしです。")
    standards = {m.rows[-1]["standard"] for m in members}
    if len(standards) > 1:
        notes.append("会計基準が混在しています（" + "、".join(
            sorted(STANDARDS.get(s or "", "—") for s in standards))
            + "）。営業利益の定義が異なるため、営業利益率・EBITDAマージンは参考値です。"
            "当期純利益率とROEを優先してください。")
    single = [short_name(m.company) for m in members if m.rows[-1]["basis"] == "non-consolidated"]
    if single and len(single) < len(members):
        notes.append("単体の数値の会社があります（" + "、".join(single) + "）。連結の会社と"
                     "範囲が異なります。")
    if any(m.rows[-1]["standard"] == "Japan GAAP" for m in members):
        notes.append("日本基準の営業利益と、各社の減価償却費は財務諸表から読むため、最新2期分だけです。")
    notes += [
        ("中央値・順位は値のある会社だけで計算しています。順位は値の大きい順で、良し悪しの順では"
         "ありません。"),
        ("時価総額・PER・PBRは各社の期末時点の値です（PERは有価証券報告書の記載で、赤字の期は"
         "空欄）。現在の株価に基づく値ではありません。"),
        ("ネットキャッシュ（有利子負債）・設備投資・研究開発費・セグメント別の数値は収録していません。"
         "必要なら有価証券報告書の本文から読んでください。"),
    ]
    return notes


def cmd_filings(ctx: Context, args: argparse.Namespace) -> tuple[str, Any]:
    company = resolve(ctx.companies, args.query)
    types = [t.strip() for t in (args.types or "").split(",") if t.strip()]
    sql = "SELECT * FROM filings WHERE edinet_code = ?"
    params: list[Any] = [company["edinet_code"]]
    if types:
        sql += f" AND doc_type_code IN ({','.join('?' for _ in types)})"
        params += types
    sql += " ORDER BY submitted_at DESC, doc_id LIMIT ?"
    rows = [dict(r) for r in ctx.db.execute(sql, [*params, args.limit])]
    title = f"# {label(company)} の提出書類"
    if not rows:
        return ((f"{title}\n\n収録期間（{ctx.source['filings_window']}）に該当する書類はありません。"
                 "それ以降の提出は edinet-util の list_filings で探してください。"),
                {"company": company, "filings": []})
    body = [[r["doc_id"], r["submitted_at"] or "—",
             DOC_TYPES.get(r["doc_type_code"] or "", r["doc_type_code"] or "—"),
             r["description"] or "—",
             "〜".join(p for p in (r["period_start"], r["period_end"]) if p) or "—",
             "・".join(n for n, flag in (("XBRL", r["xbrl"]), ("PDF", r["pdf"]),
                                         ("英文", r["english"])) if flag) or "—"]
            for r in rows]
    text = "\n".join([
        title, "",
        table(["書類管理番号", "提出日時", "種別", "書類名", "対象期間", "形式"], body), "",
        ("書類管理番号を edinet-util の export_filing に渡すと、本文を Markdown で読めます。"
         f"収録は {ctx.source['filings_window']} の提出分です。それ以降の提出や、"
         "取り下げ・訂正の有無は list_filings で確かめてください。"),
    ])
    return text, {"company": company, "filings": rows}


def cmd_sql(ctx: Context, args: argparse.Namespace) -> tuple[str, Any]:
    statement = args.statement.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", statement) or ";" in statement:
        raise QueryError("SELECT（または WITH）文を1つだけ指定してください。")
    try:
        cursor = ctx.db.execute(statement)
    except sqlite3.Error as error:
        raise QueryError(f"SQLエラー: {error}") from error
    columns = [d[0] for d in cursor.description or []]
    rows = cursor.fetchmany(args.limit + 1)
    more = len(rows) > args.limit
    rows = rows[: args.limit]
    body = [["—" if v is None else v for v in row] for row in rows]
    text = table(columns, body) if columns else "（結果なし）"
    if more:
        text += f"\n\n先頭 {args.limit} 行だけを表示しました（--limit で変えられます）。"
    return text, {"columns": columns, "rows": [list(r) for r in rows], "truncated": more}


def cmd_about(ctx: Context, args: argparse.Namespace) -> tuple[str, Any]:
    counts = {row[0]: row[1] for row in ctx.db.execute(
        "SELECT profile_status, COUNT(*) FROM companies GROUP BY 1 ORDER BY 2 DESC")}
    sizes = {name: ctx.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
             for name in ("companies", "financials", "peers", "filings")}
    sources = [dict(r) for r in ctx.db.execute("SELECT * FROM sources ORDER BY source")]
    columns = {name: [r["name"] for r in ctx.db.execute(f"PRAGMA table_info({name})")]
               for name in ("manifest", "sources", "companies", "filings", "financials", "peers")}
    lines = ["# EDINET 企業プロファイル（スナップショット）", "",
             table(["項目", "値"], [[k, v] for k, v in ctx.meta.items()
                                    if k not in ("source_credit", "processing_notice")]),
             "", "## 件数", "",
             table(["表", "行数"], [[k, f"{v:,}"] for k, v in sizes.items()]), "",
             table(["状態", "企業数", "意味"],
                   [[k, f"{v:,}", STATUS_NOTES.get(k, "") or "指標・同業候補あり"]
                    for k, v in counts.items()]),
             "", "## 出所", "",
             table(["出所", "URL", "時点", "説明"],
                   [[s["source"], s["url"], s["as_of"], s["note"]] for s in sources]),
             "", "## 表と列（sql コマンド用）", ""]
    lines += [f"- **{name}**: {', '.join(cols)}" for name, cols in columns.items()]
    lines += ["", "金額は円、比率は小数（0.101 = 10.1%）。sec_code は5桁（先頭4桁が証券コード）。"]
    return "\n".join(lines), {"manifest": ctx.meta, "tables": sizes, "status": counts,
                              "sources": sources, "columns": columns}


# ---------------------------------------------------------------- main --------


COMMANDS = {"company": cmd_company, "peers": cmd_peers, "compare": cmd_compare,
            "filings": cmd_filings, "sql": cmd_sql, "about": cmd_about}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="EDINET company-profile snapshot (read-only)")
    root.add_argument("--db", type=Path, default=Path(os.environ.get("EDINET_PROFILES_DB")
                                                      or DB_PATH), help=argparse.SUPPRESS)
    sub = root.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="structured output")

    p = sub.add_parser("company", parents=[common], help="profile and 5-year figures")
    p.add_argument("query")
    p = sub.add_parser("peers", parents=[common], help="ranked peer candidates")
    p.add_argument("query")
    p.add_argument("-n", type=int, default=MAX_PEERS, choices=range(1, MAX_PEERS + 1),
                   metavar=f"1-{MAX_PEERS}")
    p = sub.add_parser("compare", parents=[common], help="metrics against peers")
    p.add_argument("query")
    p.add_argument("--peers", action="append", help="peers to include, comma-separated")
    p.add_argument("-n", type=int, default=DEFAULT_PEERS, choices=range(1, MAX_PEERS + 1),
                   metavar=f"1-{MAX_PEERS}", help="number of peers in total")
    p = sub.add_parser("filings", parents=[common], help="indexed filings with document IDs")
    p.add_argument("query")
    p.add_argument("--types", help="document type codes, e.g. 120,160")
    p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("sql", parents=[common], help="read-only SELECT")
    p.add_argument("statement")
    p.add_argument("--limit", type=int, default=DEFAULT_SQL_ROWS)
    sub.add_parser("about", parents=[common], help="snapshot, sources and tables")
    return root


def run(argv: list[str] | None = None) -> tuple[int, str]:
    args = parser().parse_args(argv)
    try:
        db = open_db(args.db)
    except QueryError as error:
        return 1, str(error)
    try:
        meta = manifest(db)
        ctx = Context(db, meta, attribution(meta, db), companies(db))
        try:
            text, data = COMMANDS[args.command](ctx, args)
            status = 0
        except QueryError as error:
            text, data, status = str(error), {"error": str(error)}, 1
        if args.json:
            payload = {"data": data, "source": ctx.source} if status == 0 else {
                **data, "source": ctx.source}
            return status, json.dumps(payload, ensure_ascii=False, indent=1, default=_json)
        return status, f"{text}\n\n{footer(ctx.source)}"
    finally:
        db.close()


def _json(value: Any) -> Any:
    if isinstance(value, Metric):
        return value.key
    raise TypeError(type(value).__name__)


def main() -> None:
    # Windows consoles default to a legacy code page; the output is Japanese.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    status, text = run()
    print(text)
    sys.exit(status)


if __name__ == "__main__":
    main()
