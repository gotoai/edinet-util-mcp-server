"""The EDINET code list (`Edinetcode.zip`): every filer, with its listing status.

The ZIP holds one CP932 CSV. Its first line is metadata (download date, count),
the second the header, then one quoted row per filer:

ＥＤＩＮＥＴコード, 提出者種別, 上場区分, 連結の有無, 資本金, 決算日, 提出者名,
提出者名（英字）, 提出者名（ヨミ）, 所在地, 提出者業種, 証券コード, 提出者法人番号

資本金 is in millions of yen; 決算日 reads like "3月31日" or "3月末日" (month end).
"""

from __future__ import annotations

import calendar
import csv
import io
import re
import zipfile
from dataclasses import dataclass

_FISCAL_YEAR_END = re.compile(r"^(\d{1,2})月(\d{1,2}|末)日$")
_AS_OF = re.compile(r"(\d{4})年(\d{2})月(\d{2})日")


@dataclass(frozen=True)
class Filer:
    edinet_code: str
    filer_type: str  # 内国法人・組合, 外国法人・組合, 個人, ...
    listing: str  # 上場, 非上場, or blank
    consolidated: str  # 有, 無, or blank
    capital_million_yen: int | None
    fiscal_year_end: str | None  # MM-DD
    name: str
    name_en: str
    name_kana: str
    address: str
    industry: str  # TSE 33-industry name
    sec_code: str
    jcn: str

    @property
    def listed(self) -> bool:
        return self.listing == "上場"

    @property
    def fiscal_year_end_month(self) -> int | None:
        return int(self.fiscal_year_end[:2]) if self.fiscal_year_end else None


@dataclass(frozen=True)
class CodeList:
    as_of: str | None  # ISO date of the download, from the file's first line
    filers: list[Filer]


def parse_codelist(zip_bytes: bytes) -> CodeList:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
        text = archive.read(name).decode("cp932")
    lines = text.splitlines()
    as_of = None
    if lines and (match := _AS_OF.search(lines[0])):
        as_of = "-".join(match.groups())
    rows = list(csv.reader(lines[2:]))
    return CodeList(as_of=as_of, filers=[_filer(row) for row in rows if len(row) >= 13])


def _filer(row: list[str]) -> Filer:
    cells = [cell.strip() for cell in row]
    return Filer(
        edinet_code=cells[0],
        filer_type=cells[1],
        listing=cells[2],
        consolidated=cells[3],
        capital_million_yen=int(cells[4]) if cells[4].isdigit() else None,
        fiscal_year_end=_fiscal_year_end(cells[5]),
        name=_normalise_name(cells[6]),
        name_en=cells[7],
        name_kana=cells[8],
        address=cells[9],
        industry=cells[10],
        sec_code=cells[11],
        jcn=cells[12],
    )


def _fiscal_year_end(value: str) -> str | None:
    match = _FISCAL_YEAR_END.match(value)
    if not match:
        return None
    month = int(match[1])
    # 末日 is the month's last day; February is given as the 28th, its usual end.
    day = calendar.monthrange(2026, month)[1] if match[2] == "末" else int(match[2])
    return f"{month:02d}-{day:02d}"


def _normalise_name(name: str) -> str:
    # The list pads some names with a full-width space ("株式会社　サカタのタネ").
    return name.replace("　", " ").strip()
