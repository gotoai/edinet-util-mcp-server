"""A skill-style MCP server for EDINET (金融庁) API v2 disclosures.

Built to Kari's skill-style MCP contract
(kari/docs/design_skill-style-mcp-server.md in the Kari repo): a one-sentence
`instructions`, a `load_guideline` tool carrying everything that concerns the
tool set as a whole — conditions of use included — and tool-specific
descriptions on the other tools.

Data access only (design D17): company profiles and peers live in the
edinet-company-profiles skill. The only writes are exports into the output folder
fixed by EDINET_OUTPUT_DIR (design D5).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import api, export
from .guideline import GUIDELINE

SERVER_NAME = "edinet-util"

INSTRUCTIONS = (
    "EDINET（金融庁の開示システム）の提出書類を日付で探し、有価証券報告書などを Markdown に"
    "変換して読むツール群です。日本の上場企業の開示書類を調べるときに使い、最初に "
    "load_guideline を呼んでください。"
)

DEFAULT_OUTPUT_DIR = "~/Documents/EDINET"
MAX_DAYS = 31
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
HISTORY_YEARS = 10
TODAY_LIST_TTL = 5 * 60
PAST_LIST_TTL = 6 * 60 * 60

DocType = Literal[
    "010", "020", "030", "040", "050", "060", "070", "080", "090", "100", "110", "120",
    "130", "135", "136", "140", "150", "160", "170", "180", "190", "200", "210", "220",
    "230", "235", "236", "240", "250", "260", "270", "280", "290", "300", "310", "320",
    "330", "340", "350", "360", "370", "380",
]
DOC_TYPE_NAMES = {
    "120": "有価証券報告書", "130": "訂正有価証券報告書", "135": "確認書", "136": "訂正確認書",
    "140": "四半期報告書", "150": "訂正四半期報告書", "160": "半期報告書",
    "170": "訂正半期報告書", "180": "臨時報告書", "190": "訂正臨時報告書",
    "220": "自己株券買付状況報告書", "230": "訂正自己株券買付状況報告書",
    "235": "内部統制報告書", "236": "訂正内部統制報告書", "030": "有価証券届出書",
    "040": "訂正有価証券届出書", "240": "公開買付届出書", "270": "公開買付報告書",
    "290": "意見表明報告書", "350": "大量保有報告書", "360": "訂正大量保有報告書",
}

mcp = MCPServer(
    SERVER_NAME,
    title="EDINET 開示書類ユーティリティ",
    description="Unofficial MCP server for the EDINET API v2. Not affiliated with the "
                "Financial Services Agency.",
    version=version("edinet-util-mcp-server"),
    instructions=INSTRUCTIONS,
)

_client: api.EdinetClient | None = None
# The EDINET client is synchronous and paced; one request at a time, off the loop.
_lock = asyncio.Lock()
_lists: dict[str, tuple[float, dict[str, Any]]] = {}

_LIVE = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _api() -> api.EdinetClient:
    global _client
    if _client is None:
        try:
            key = api.load_api_key()
        except api.EdinetError as error:
            raise ToolError(
                "EDINET_API_KEY が設定されていません。この拡張機能の環境変数に、EDINET API の"
                "キー（https://api.edinet-fsa.go.jp/ で取得）を設定してください。") from error
        _client = api.EdinetClient(key)
    return _client


async def _call(function: Any, *args: Any) -> Any:
    async with _lock:
        return await asyncio.to_thread(function, *args)


def output_dir() -> Path:
    return Path(os.environ.get("EDINET_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR).expanduser()


def _source(doc_id: str | None = None) -> dict[str, str]:
    source = {"credit": export.SOURCE_CREDIT}
    if doc_id:
        source["processing_notice"] = export.processing_notice(doc_id)
        source["document_url"] = export.document_url(doc_id)
    return source


@mcp.tool(
    description=(
        "Usage guideline for the EDINET tools: how to find filings, how to read an exported "
        "filing, how they relate to the company-profile skill, and the conditions of use. "
        "Read it before your first search."
    ),
    annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
)
async def load_guideline() -> str:
    return GUIDELINE


@mcp.tool(
    description=(
        "List EDINET filings submitted between two dates (at most 31 days, about 1 second per "
        "day), optionally only one company's or only some document types. Returns each "
        "filing's doc_id for export_filing, newest first."
    ),
    annotations=_LIVE,
)
async def list_filings(
    from_date: Annotated[str, Field(description="First submission date, YYYY-MM-DD.")],
    to_date: Annotated[
        str | None, Field(description="Last submission date, YYYY-MM-DD; default from_date.")
    ] = None,
    edinet_code: Annotated[
        str | None, Field(description="Only this filer, e.g. E01152.", max_length=6)
    ] = None,
    sec_code: Annotated[
        str | None, Field(description="Only this securities code, e.g. 5208.", max_length=6)
    ] = None,
    filer_name: Annotated[
        str | None, Field(description="Only filers whose name contains this.", max_length=100)
    ] = None,
    doc_types: Annotated[
        list[DocType] | None, Field(description="Document type codes, e.g. ['120', '160'].")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> dict[str, Any]:
    start, end = _date(from_date), _date(to_date or from_date)
    today = datetime.now(export.JST).date()
    if end < start:
        raise ToolError("to_date must not be before from_date.")
    if (end - start).days + 1 > MAX_DAYS:
        raise ToolError(f"The range is at most {MAX_DAYS} days; split it into several calls.")
    if end > today:
        raise ToolError(f"to_date is in the future (today is {today.isoformat()} JST).")
    if start < today.replace(year=today.year - HISTORY_YEARS):
        raise ToolError(f"EDINET lists filings of the last {HISTORY_YEARS} years only.")
    code = (edinet_code or "").strip().upper()
    if code and not re.fullmatch(r"E\d{5}", code):
        raise ToolError("edinet_code looks like E01152.")
    security = unicodedata.normalize("NFKC", sec_code or "").strip().upper().removesuffix(".T")
    if security and not re.fullmatch(r"[0-9][0-9A-Z]{3}[0-9]?", security):
        raise ToolError("sec_code looks like 5208 (4 characters, as quoted).")
    name = unicodedata.normalize("NFKC", filer_name or "").strip().lower()
    types = set(doc_types or [])

    matches = []
    day = start
    while day <= end:
        for result in (await _daily_list(day, today)).get("results") or []:
            if code and result.get("edinetCode") != code:
                continue
            if security and (result.get("secCode") or "")[:4] != security[:4]:
                continue
            if name and name not in unicodedata.normalize(
                    "NFKC", result.get("filerName") or "").lower():
                continue
            if types and result.get("docTypeCode") not in types:
                continue
            matches.append(_filing(result))
        day += timedelta(days=1)
    matches.sort(key=lambda f: (f["submitted_at"] or "", f["doc_id"]), reverse=True)
    return {
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "total_count": len(matches),
        "truncated": len(matches) > limit,
        "filings": matches[:limit],
        "source": _source(),
    }


async def _daily_list(day: date, today: date) -> dict[str, Any]:
    key = day.isoformat()
    ttl = TODAY_LIST_TTL if day == today else PAST_LIST_TTL
    cached = _lists.get(key)
    if cached and time.monotonic() - cached[0] < ttl:
        return cached[1]
    client = _api()
    try:
        data = await _call(client.daily_list, key)
    except api.EdinetError as error:
        raise ToolError(str(error)) from error
    _lists[key] = (time.monotonic(), data)
    return data


def _filing(r: dict[str, Any]) -> dict[str, Any]:
    sec = r.get("secCode") or ""
    doc_type = r.get("docTypeCode") or ""
    return {
        "doc_id": r.get("docID"),
        "submitted_at": r.get("submitDateTime"),
        "edinet_code": r.get("edinetCode"),
        "sec_code": sec[:4] if len(sec) == 5 else sec or None,
        "filer_name": r.get("filerName"),
        "doc_type_code": doc_type,
        "doc_type": DOC_TYPE_NAMES.get(doc_type),
        "description": r.get("docDescription"),
        "period_start": r.get("periodStart"),
        "period_end": r.get("periodEnd"),
        "parent_doc_id": r.get("parentDocID"),
        "withdrawn": r.get("withdrawalStatus") not in (None, "0"),
        "disclosed": r.get("disclosureStatus") not in ("1", "2"),
        "xbrl": r.get("xbrlFlag") == "1",
        "pdf": r.get("pdfFlag") == "1",
    }


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except ValueError as error:
        raise ToolError(f"{text!r} is not a date in YYYY-MM-DD form.") from error


@mcp.tool(
    description=(
        "Download one filing by doc_id and save it as Markdown files in the EDINET output "
        "folder: index.md (metadata and contents), figures.md (5-year key figures) and one "
        "file per section. Returns the folder and file list; read the files you need. A "
        "filing already exported is returned without downloading again."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                idempotentHint=True, openWorldHint=True),
)
async def export_filing(
    doc_id: Annotated[str, Field(description="The filing's doc_id, e.g. S100YCDM.",
                                 max_length=8)],
    refresh: Annotated[bool, Field(description="Download and convert again.")] = False,
) -> dict[str, Any]:
    folder = output_dir()
    doc_id = doc_id.strip().upper()
    existing = export.find_existing(folder, doc_id)
    try:
        if existing and not refresh:
            result = export.export(_no_download, doc_id, folder)
        else:
            client = _api()
            async with _lock:
                result = await asyncio.to_thread(export.export, client.document, doc_id,
                                                 folder, refresh=refresh)
    except export.ExportError as error:
        raise ToolError(str(error)) from error
    except api.EdinetError as error:
        raise ToolError(str(error)) from error
    meta = result.metadata
    names = [f["path"] for f in result.files]
    return {
        "folder": str(result.folder),
        "index": str(result.folder / "index.md"),
        "figures": str(result.folder / "figures.md") if "figures.md" in names else None,
        "reused": result.reused,
        "company": meta.get("company"),
        "document": meta.get("document"),
        "period_end": meta.get("period_end"),
        "files": [f for f in result.files if f["path"] != "index.md"],
        "source": _source(doc_id),
    }


def _no_download(doc_id: str, doc_type: int) -> bytes:
    raise AssertionError("an existing export needs no download")


def main() -> None:
    parser = argparse.ArgumentParser(description="EDINET skill-style MCP server")
    # stdio only: the server writes into the local output folder of the person who
    # runs it, and uses that person's own API key (design D1).
    parser.add_argument("--transport", choices=["stdio"], default="stdio")
    parser.parse_args()
    mcp.run("stdio")


if __name__ == "__main__":
    main()
