"""One filing → a folder of Markdown files the user and the agent both read (design D4).

    <output dir>/<EDINET code>_<company>/<docID>_<document type>_<period>/
      index.md        front matter, period mapping, table of contents
      figures.md      the 5-year key figures recomputed from the XBRL-to-CSV file
      sections/       one file per sub-section, e.g. 0102010-02_事業等のリスク.md
      images/         the filing's images, linked from the sections

Every file carries the source credit and the processing notice at its top and its
bottom (design D15). A finished folder is reused on the next request, so a filing
costs API calls once; a folder is written beside its final place and renamed into
it, so an interrupted export leaves nothing half-written behind.
"""

from __future__ import annotations

import io
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from lxml import etree

from . import api, convert, figures

JST = timezone(timedelta(hours=9), "JST")
DOC_ID = re.compile(r"S[0-9A-Z]{7}")
SOURCE_CREDIT = ("出典：EDINET閲覧（提出）サイト（https://disclosure2.edinet-fsa.go.jp/）、"
                 "PDL1.0（https://www.digital.go.jp/resources/open_data/public_data_license_v1.0）")
PDF_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{doc_id}?type=2"

FIGURE_ROWS: tuple[tuple[str, str, str], ...] = (
    ("revenue", "売上高等", "yen"), ("operating_income", "営業利益", "yen"),
    ("ordinary_income", "経常利益", "yen"), ("profit_before_tax", "税引前利益", "yen"),
    ("net_income", "親会社に帰属する当期純利益", "yen"),
    ("comprehensive_income", "包括利益", "yen"), ("net_assets", "純資産", "yen"),
    ("total_assets", "総資産", "yen"), ("equity_ratio", "自己資本比率", "pct"),
    ("roe", "自己資本利益率（ROE）", "pct"), ("eps", "1株当たり当期純利益（円）", "per_share"),
    ("bps", "1株当たり純資産（円）", "per_share"), ("per", "株価収益率（倍）", "times"),
    ("dps", "1株当たり配当額（円）※", "per_share"), ("payout_ratio", "配当性向※", "pct"),
    ("operating_cf", "営業活動によるキャッシュ・フロー", "yen"),
    ("investing_cf", "投資活動によるキャッシュ・フロー", "yen"),
    ("financing_cf", "財務活動によるキャッシュ・フロー", "yen"),
    ("cash", "現金及び現金同等物の期末残高", "yen"), ("depreciation", "減価償却費", "yen"),
    ("issued_shares", "発行済株式総数（株）", "count"),
)


class ExportError(RuntimeError):
    """A message to pass on to the agent as it is."""


@dataclass
class Export:
    folder: Path
    files: list[dict[str, Any]]
    reused: bool
    metadata: dict[str, Any]


def processing_notice(doc_id: str) -> str:
    return (f"EDINET閲覧（提出）サイトの提出書類（書類管理番号 {doc_id}）をもとに "
            "edinet-util-mcp-server が加工して作成。原本ではありません。")


def document_url(doc_id: str) -> str:
    return api.DOCUMENT_PAGE_URL.format(doc_id=doc_id)


def find_existing(output_dir: Path, doc_id: str) -> Path | None:
    for index in output_dir.glob(f"*/{doc_id}_*/index.md"):
        return index.parent
    return None


def export(download: Callable[[str, int], bytes], doc_id: str, output_dir: Path, *,
           refresh: bool = False, now: datetime | None = None) -> Export:
    """Export one filing. `download(doc_id, type)` returns a document's file."""
    doc_id = doc_id.strip().upper()
    if not DOC_ID.fullmatch(doc_id):
        raise ExportError(f"{doc_id!r} は書類管理番号の形式（S で始まる8文字）ではありません。")
    existing = find_existing(output_dir, doc_id)
    if existing and not refresh:
        return Export(existing, _listing(existing), True, _read_front_matter(existing))

    try:
        body = download(doc_id, api.DOC_TYPE_ZIP)
    except api.DocumentUnavailable as error:
        raise ExportError(
            f"{doc_id} は EDINET から取得できません（{error}）。取り下げ・非開示・閲覧期間の"
            "終了で一覧から消えた可能性があります。list_filings で今の提出状況を確かめてください。"
        ) from error
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except zipfile.BadZipFile as error:
        raise ExportError(f"{doc_id} の書類ファイルを開けませんでした。") from error
    with archive:
        chapters = sorted(n for n in archive.namelist()
                          if n.startswith("XBRL/PublicDoc/") and n.endswith("_ixbrl.htm"))
        if not chapters:
            raise ExportError(
                f"{doc_id} には XBRL（本文のHTML）がありません。PDF で開いてください："
                f"{document_url(doc_id)}")
        meta = _metadata(archive, chapters, doc_id, now or datetime.now(JST))
        try:
            facts = figures.parse_facts(figures.report_csv_text(
                download(doc_id, api.DOC_TYPE_CSV)))
        except (api.DocumentUnavailable, ValueError, zipfile.BadZipFile):
            facts = []
        folder = output_dir / _safe(f"{meta['edinet_code']}_{meta['company']}") / _safe(
            f"{doc_id}_{meta['document']}_{(meta['period_end'] or '')[:7]}".rstrip("_"))
        folder.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f".{doc_id}.", dir=folder.parent))
        try:
            _write(work, archive, chapters, meta, facts)
            if existing:
                stale = existing.with_name(existing.name + ".old")
                existing.rename(stale)
                shutil.rmtree(stale)
            if folder.exists():
                shutil.rmtree(folder)
            work.rename(folder)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
    return Export(folder, _listing(folder), False, meta)


def _write(folder: Path, archive: zipfile.ZipFile, chapters: list[str],
           meta: dict[str, Any], facts: list[figures.Fact]) -> None:
    doc_id = meta["doc_id"]
    top, bottom = _attribution(doc_id)
    (folder / "sections").mkdir()
    toc: list[str] = []
    images: set[str] = set()
    for name in chapters:
        chapter = PurePosixPath(name).name[:7]
        for section in convert.sections(archive.read(name), chapter):
            file = f"{chapter}-{section.index:02d}_{_safe(section.title)[:60]}.md"
            text = f"{top}\n\n{section.markdown}\n{bottom}\n"
            (folder / "sections" / file).write_text(text, encoding="utf-8")
            images.update(section.images)
            headings = [line.lstrip("#").strip() for line in section.markdown.splitlines()
                        if re.match(r"#{1,2} ", line)]
            toc.append(f"- [{section.title}](sections/{file})（{len(section.markdown):,}字）")
            toc += [f"  - {h}" for h in headings[1:8]]
            if len(headings) > 8:
                toc.append(f"  - ほか {len(headings) - 8} 見出し")

    if images:
        (folder / "images").mkdir()
    for image in sorted(images):
        source = f"XBRL/PublicDoc/{image}"
        if source in archive.namelist():
            (folder / "images" / PurePosixPath(image).name).write_bytes(archive.read(source))

    figures_file = None
    if facts:
        annual = figures.extract(facts)
        table = _figures_markdown(annual)
        if table:
            figures_file = "figures.md"
            (folder / figures_file).write_text(
                f"{top}\n\n# 経営指標等（{meta['company']}）\n\n{table}\n\n{bottom}\n",
                encoding="utf-8")

    lines = ["---"]
    lines += [f"{key}: {_yaml(value)}" for key, value in meta.items()]
    lines += [f"source_credit: {_yaml(SOURCE_CREDIT)}",
              f"processing_notice: {_yaml(processing_notice(doc_id))}", "---", "",
              top, "", f"# {meta['company']}　{meta['document_title'] or meta['document']}", ""]
    lines += [f"- {label}：{value}" for label, value in _period_lines(meta)]
    lines += ["- 金額の単位は表ごとに異なります。各表の上の（単位：…）を見てください。",
              "- 監査報告書と添付書類は含みません。", ""]
    if figures_file:
        lines += [f"[経営指標等（5年、CSVから再計算）]({figures_file})", ""]
    lines += ["## 目次", "", *toc, "", bottom, ""]
    (folder / "index.md").write_text("\n".join(lines), encoding="utf-8")


def _attribution(doc_id: str) -> tuple[str, str]:
    notice = processing_notice(doc_id)
    url = document_url(doc_id)
    top = f"> {SOURCE_CREDIT}\n> {notice}\n> 原本：{url}"
    bottom = f"---\n\n{SOURCE_CREDIT}\n\n{notice}\n原本：{url}"
    return top, bottom


def _metadata(archive: zipfile.ZipFile, chapters: list[str], doc_id: str,
              now: datetime) -> dict[str, Any]:
    dei: dict[str, str] = {}
    instances = [n for n in archive.namelist()
                 if n.startswith("XBRL/PublicDoc/") and n.endswith(".xbrl")]
    if instances:
        root = etree.fromstring(archive.read(instances[0]),
                                etree.XMLParser(huge_tree=True, recover=True))
        for element in root:
            if isinstance(element.tag, str) and "jpdei" in element.tag:
                dei[element.tag.rsplit("}", 1)[-1]] = (element.text or "").strip()
    cover = _cover(archive, chapters)
    consolidated = dei.get("WhetherConsolidatedFinancialStatementsArePreparedDEI") == "true"
    sec_code = dei.get("SecurityCodeDEI") or ""
    return {
        "doc_id": doc_id,
        "company": dei.get("FilerNameInJapaneseDEI") or cover.get("会社名") or doc_id,
        "edinet_code": dei.get("EDINETCodeDEI") or "",
        "sec_code": sec_code[:4] if len(sec_code) == 5 else sec_code,
        "document": cover.get("提出書類") or dei.get("DocumentTypeDEI") or "書類",
        "document_title": cover.get("事業年度") or cover.get("中間会計期間") or "",
        "submitted": cover.get("提出日") or "",
        "accounting_standard": dei.get("AccountingStandardsDEI") or "",
        "basis": "consolidated" if consolidated else "non-consolidated",
        "period_type": dei.get("TypeOfCurrentPeriodDEI") or "",
        "period_start": dei.get("CurrentFiscalYearStartDateDEI") or "",
        "period_end": dei.get("CurrentPeriodEndDateDEI")
        or dei.get("CurrentFiscalYearEndDateDEI") or "",
        "previous_start": dei.get("PreviousFiscalYearStartDateDEI") or "",
        "previous_end": dei.get("PreviousFiscalYearEndDateDEI") or "",
        "amendment": dei.get("AmendmentFlagDEI") == "true",
        "retrieved_at": now.isoformat(timespec="seconds"),
        "source_url": document_url(doc_id),
    }


def _cover(archive: zipfile.ZipFile, chapters: list[str]) -> dict[str, str]:
    name = next((n for n in chapters if PurePosixPath(n).name.startswith("0000000")), None)
    if not name:
        return {}
    text = convert.sections(archive.read(name), "0000000")
    values: dict[str, str] = {}
    for section in text:
        for match in re.finditer(r"^\| 【([^】]+)】 \| (.*?) \|$", section.markdown,
                                 re.MULTILINE):
            values.setdefault(match.group(1), match.group(2).replace("<br>", " ").strip())
    return values


def _period_lines(meta: dict[str, Any]) -> list[tuple[str, str]]:
    current = "当連結会計年度" if meta["basis"] == "consolidated" else "当事業年度"
    previous = "前連結会計年度" if meta["basis"] == "consolidated" else "前事業年度"
    lines = [("書類管理番号", meta["doc_id"]), ("提出日", meta["submitted"] or "—"),
             ("EDINETコード", meta["edinet_code"] or "—"), ("証券コード", meta["sec_code"] or "—"),
             ("会計基準", meta["accounting_standard"] or "—"),
             ("数値の範囲", "連結" if meta["basis"] == "consolidated" else "単体（連結財務諸表なし）")]
    if meta["period_start"] and meta["period_end"]:
        lines.append((current, f"{meta['period_start']}〜{meta['period_end']}"))
    if meta["previous_start"] and meta["previous_end"]:
        lines.append((previous, f"{meta['previous_start']}〜{meta['previous_end']}"))
    lines.append(("取得日時", meta["retrieved_at"]))
    return lines


def _figures_markdown(annual: figures.AnnualFigures) -> str:
    years = list(reversed(annual.years))
    if not years:
        return ""
    header = ["項目"] + [y.period_end or f"{y.offset}期前" for y in years]
    rows = []
    for key, label, unit in FIGURE_ROWS:
        values = [y.values.get(key) for y in years]
        if all(v is None for v in values):
            continue
        shown = annual.revenue_label if key == "revenue" and annual.revenue_label else label
        rows.append([shown] + [_figure(unit, v) for v in values])
    if not rows:
        return ""
    basis = "連結" if annual.consolidated else "単体"
    lines = [(f"数値の範囲：{basis}。会計基準：{annual.accounting_standard or '—'}。"
              "金額は百万円（原本の単位から換算）、比率は%。"),
             ("この表は XBRL を CSV に変換したファイルから再計算したもので、原本の表ではありません。"
              "営業利益（日本基準）と減価償却費は財務諸表から読むため、最新2期だけです。"
              "※の配当は、連結の記載がない会社では提出会社（単体）の値です。"), "",
             "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    if annual.employees is not None:
        lines += ["", f"従業員数（最新期末）：{annual.employees:,}人"]
    return "\n".join(lines)


def _figure(unit: str, value: float | None) -> str:
    if value is None:
        return "—"
    if unit == "yen":
        return f"{value / 1_000_000:,.0f}"
    if unit == "pct":
        return f"{value * 100:.1f}%"
    if unit == "count":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def _listing(folder: Path) -> list[dict[str, Any]]:
    files = []
    for path in sorted(folder.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = next((line.lstrip("#").strip() for line in text.splitlines()
                      if line.startswith("#")), path.stem)
        files.append({"path": path.relative_to(folder).as_posix(), "title": title,
                      "chars": len(text)})
    return files


def _read_front_matter(folder: Path) -> dict[str, Any]:
    text = (folder / "index.md").read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    if text.startswith("---\n"):
        for line in text[4:text.find("\n---", 4)].splitlines():
            key, _, value = line.partition(": ")
            meta[key] = value.strip('"')
    return meta


def _yaml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _safe(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name)
    return re.sub(r"\s+", " ", name).strip(" .") or "_"
