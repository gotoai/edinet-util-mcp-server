"""The MCP server: Kari's skill-style contract, and the tools against a fake EDINET."""

import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from mcp import Client

from edinet_util_mcp_server import api, export, server
from edinet_util_mcp_server.guideline import GUIDELINE, SECTIONS
from edinet_util_mcp_server.server import mcp
from fixtures import daily_list, japan_gaap_report, list_result

TOOL_DESCRIPTION_TARGET = 1000
IX = 'xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"'
PREFIX = "jpcrp030000-asr-001_E00001-000_2026-03-31_01_2026-06-26"


def filing_zip() -> bytes:
    def page(body: str) -> bytes:
        return f"<html {IX}><body>{body}</body></html>".encode()
    cover = page("<h1>【表紙】</h1><table>"
                 "<tr><td>【提出書類】</td><td>有価証券報告書</td></tr>"
                 "<tr><td>【提出日】</td><td>2026年6月26日</td></tr>"
                 "<tr><td>【会社名】</td><td>株式会社テスト</td></tr></table>")
    business = page("<h2>第２【事業の状況】</h2><h3>１【経営方針】</h3><p>方針です。</p>"
                    "<h3>２【事業等のリスク】</h3><p>リスクです。</p>"
                    '<img src="images/0102010_001.png"/>')
    instance = ('<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
                'xmlns:jpdei_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jpdei/2013-08-31/'
                'jpdei_cor">'
                '<jpdei_cor:EDINETCodeDEI contextRef="FilingDateInstant">E00001'
                "</jpdei_cor:EDINETCodeDEI>"
                '<jpdei_cor:SecurityCodeDEI contextRef="FilingDateInstant">52080'
                "</jpdei_cor:SecurityCodeDEI>"
                '<jpdei_cor:FilerNameInJapaneseDEI contextRef="FilingDateInstant">株式会社テスト'
                "</jpdei_cor:FilerNameInJapaneseDEI>"
                '<jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI '
                'contextRef="FilingDateInstant">true'
                "</jpdei_cor:WhetherConsolidatedFinancialStatementsArePreparedDEI>"
                '<jpdei_cor:CurrentFiscalYearStartDateDEI contextRef="FilingDateInstant">'
                "2025-04-01</jpdei_cor:CurrentFiscalYearStartDateDEI>"
                '<jpdei_cor:CurrentPeriodEndDateDEI contextRef="FilingDateInstant">2026-03-31'
                "</jpdei_cor:CurrentPeriodEndDateDEI>"
                "</xbrli:xbrl>")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"XBRL/PublicDoc/0000000_header_{PREFIX}_ixbrl.htm", cover)
        archive.writestr(f"XBRL/PublicDoc/0102010_honbun_{PREFIX}_ixbrl.htm", business)
        archive.writestr(f"XBRL/PublicDoc/{PREFIX}.xbrl", instance)
        archive.writestr("XBRL/PublicDoc/images/0102010_001.png", b"\x89PNG")
    return buffer.getvalue()


class FakeEdinet:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def daily_list(self, day: str):
        self.calls.append(("list", day))
        if day == "2026-06-26":
            return daily_list([
                list_result("S100AAAA", "E00001", secCode="52080", filerName="株式会社テスト",
                            submitDateTime="2026-06-26 15:00"),
                list_result("S100BBBB", "E00002", doc_type="180", filerName="別会社",
                            submitDateTime="2026-06-26 16:00"),
                list_result("S100CCCC", "E00001", withdrawalStatus="1", secCode="52080",
                            filerName="株式会社テスト", submitDateTime="2026-06-26 09:00"),
            ])
        return daily_list([])

    def document(self, doc_id: str, doc_type: int) -> bytes:
        self.calls.append(("document", doc_id, doc_type))
        if doc_id == "S100GONE":
            raise api.DocumentUnavailable(f"{doc_id} type {doc_type}: 404 Not Found")
        if doc_type == api.DOC_TYPE_CSV:
            return japan_gaap_report()
        return filing_zip()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    client = FakeEdinet()
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_lists", {})
    monkeypatch.setenv("EDINET_OUTPUT_DIR", str(tmp_path / "EDINET"))
    return client


def structured(result):
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


# ---- contract -------------------------------------------------------------------------

async def test_instructions_are_short_and_point_to_the_guideline():
    async with Client(mcp) as client:
        instructions = client.instructions or ""
    assert 0 < len(instructions) <= 200, len(instructions)
    assert "load_guideline" in instructions


async def test_server_name_is_stable_ascii_and_title_is_set():
    async with Client(mcp) as client:
        info = client.server_info
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", info.name), info.name
    assert info.title


async def test_every_tool_description_is_short_and_tool_specific():
    async with Client(mcp) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == {"load_guideline", "list_filings", "export_filing"}
    for tool in tools:
        description = tool.description or ""
        assert 0 < len(description) <= TOOL_DESCRIPTION_TARGET, (tool.name, len(description))
        assert "\n" not in description, tool.name
        if tool.name != "load_guideline":
            assert "出典" not in description, f"{tool.name}: conditions of use belong in the guideline"


async def test_guideline_is_whole_and_carries_the_conditions_of_use():
    async with Client(mcp) as client:
        tool = {t.name: t for t in (await client.list_tools()).tools}["load_guideline"]
        result = await client.call_tool("load_guideline", {})
    assert tool.annotations and tool.annotations.read_only_hint is True
    assert "before your first search" in (tool.description or "")
    assert not tool.input_schema.get("properties")
    assert result.content[0].text == GUIDELINE
    assert all(section in GUIDELINE for section in SECTIONS.values())
    assert export.SOURCE_CREDIT in GUIDELINE
    assert "edinet-company-profiles" in GUIDELINE


# ---- list_filings ---------------------------------------------------------------------

async def test_list_filings_filters_by_company_and_flags_withdrawals(fake):
    async with Client(mcp) as client:
        data = structured(await client.call_tool(
            "list_filings", {"from_date": "2026-06-25", "to_date": "2026-06-26",
                             "sec_code": "5208.T"}))
    assert [f["doc_id"] for f in data["filings"]] == ["S100AAAA", "S100CCCC"]
    assert data["filings"][0]["sec_code"] == "5208"
    assert data["filings"][1]["withdrawn"] is True
    assert data["source"]["credit"] == export.SOURCE_CREDIT
    assert fake.calls == [("list", "2026-06-25"), ("list", "2026-06-26")]


async def test_list_filings_by_document_type_and_name(fake):
    async with Client(mcp) as client:
        data = structured(await client.call_tool(
            "list_filings", {"from_date": "2026-06-26", "doc_types": ["180"],
                             "filer_name": "別"}))
    assert [f["doc_id"] for f in data["filings"]] == ["S100BBBB"]
    assert data["filings"][0]["doc_type"] == "臨時報告書"


async def test_daily_lists_are_cached_within_a_session(fake):
    async with Client(mcp) as client:
        for _ in range(2):
            await client.call_tool("list_filings", {"from_date": "2026-06-26"})
    assert fake.calls == [("list", "2026-06-26")]


@pytest.mark.parametrize("arguments, message", [
    ({"from_date": "2026-06-01", "to_date": "2026-07-15"}, "at most 31 days"),
    ({"from_date": "2026-06-26", "to_date": "2026-06-01"}, "must not be before"),
    ({"from_date": "26/06/2026"}, "YYYY-MM-DD"),
    ({"from_date": "2026-06-26", "edinet_code": "1234"}, "E02144"),
])
async def test_list_filings_rejects_bad_ranges_and_codes(fake, arguments, message):
    async with Client(mcp) as client:
        result = await client.call_tool("list_filings", arguments)
    assert result.is_error
    assert message in result.content[0].text
    assert fake.calls == []


# ---- export_filing --------------------------------------------------------------------

async def test_export_filing_writes_the_folder_and_reuses_it(fake, tmp_path):
    async with Client(mcp) as client:
        first = structured(await client.call_tool("export_filing", {"doc_id": "s100aaaa"}))
        second = structured(await client.call_tool("export_filing", {"doc_id": "S100AAAA"}))
    folder = tmp_path / "EDINET" / "E00001_株式会社テスト" / "S100AAAA_有価証券報告書_2026-03"
    assert first["folder"] == str(folder) and first["reused"] is False
    assert second["reused"] is True
    assert [c for c in fake.calls if c[0] == "document"] == [
        ("document", "S100AAAA", api.DOC_TYPE_ZIP), ("document", "S100AAAA", api.DOC_TYPE_CSV)]
    paths = [f["path"] for f in first["files"]]
    assert paths == ["figures.md", "sections/0000000-00_表紙.md",
                     "sections/0102010-00_経営方針.md", "sections/0102010-01_事業等のリスク.md"]
    assert (folder / "images" / "0102010_001.png").exists()
    risk = (folder / "sections" / "0102010-01_事業等のリスク.md").read_text(encoding="utf-8")
    assert "![図](../images/0102010_001.png)" in risk
    assert first["source"]["document_url"].endswith("WZEK0040.aspx?S100AAAA")


async def test_every_exported_file_carries_the_attribution_top_and_bottom(fake, tmp_path):
    async with Client(mcp) as client:
        data = structured(await client.call_tool("export_filing", {"doc_id": "S100AAAA"}))
    notice = export.processing_notice("S100AAAA")
    top = f"> {export.SOURCE_CREDIT}\n> {notice}\n"
    for path in [data["index"], *(f"{data['folder']}/{f['path']}" for f in data["files"])]:
        text = Path(path).read_text(encoding="utf-8")
        body = text.split("\n---\n\n", 1)[1] if text.startswith("---\n") else text
        assert body.startswith(top), path
        tail = text[-400:]
        assert export.SOURCE_CREDIT in tail and notice in tail, path


async def test_index_front_matter_maps_the_periods(fake):
    async with Client(mcp) as client:
        data = structured(await client.call_tool("export_filing", {"doc_id": "S100AAAA"}))
    index = Path(data["index"]).read_text(encoding="utf-8")
    assert 'sec_code: "5208"' in index
    assert "当連結会計年度：2025-04-01〜2026-03-31" in index
    assert "提出日：2026年6月26日" in index


async def test_a_document_no_longer_listed_gets_a_plain_message(fake):
    async with Client(mcp) as client:
        result = await client.call_tool("export_filing", {"doc_id": "S100GONE"})
    assert result.is_error
    assert "list_filings" in result.content[0].text


async def test_missing_api_key_is_explained(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    monkeypatch.setenv("EDINET_OUTPUT_DIR", str(tmp_path))
    async with Client(mcp) as client:
        result = await client.call_tool("export_filing", {"doc_id": "S100AAAA"})
    assert result.is_error
    assert "EDINET_API_KEY" in result.content[0].text
