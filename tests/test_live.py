"""Against the real EDINET API over a real stdio session. Run with EDINET_LIVE=1.

The key comes from EDINET_API_KEY, else from the repository's .env; it is passed
to the server process and never printed.
"""

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from edinet_util_mcp_server import api, export

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("EDINET_LIVE") != "1", reason="set EDINET_LIVE=1"),
]

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
ARISAWA_ANNUAL_REPORT = "S100YCDM"


@pytest.fixture
def stdio_server(tmp_path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", "edinet_util_mcp_server"],
        env={"EDINET_API_KEY": api.load_api_key(ENV_FILE),
             "EDINET_OUTPUT_DIR": str(tmp_path / "EDINET")})


def structured(result):
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


async def test_stdio_handshake_lists_a_day_and_exports_arisawa(stdio_server, tmp_path):
    async with Client(stdio_server, read_timeout_seconds=300) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert tools == {"load_guideline", "list_filings", "export_filing"}

        listed = structured(await client.call_tool(
            "list_filings", {"from_date": "2026-06-17", "sec_code": "5208",
                             "doc_types": ["120"]}))
        assert [f["doc_id"] for f in listed["filings"]] == [ARISAWA_ANNUAL_REPORT]

        exported = structured(await client.call_tool(
            "export_filing", {"doc_id": ARISAWA_ANNUAL_REPORT}))
    folder = Path(exported["folder"])
    assert folder.is_relative_to(tmp_path / "EDINET")
    assert exported["company"] == "株式会社有沢製作所"
    paths = [f["path"] for f in exported["files"]]
    assert "figures.md" in paths
    assert any("事業等のリスク" in p for p in paths)
    figures = (folder / "figures.md").read_text(encoding="utf-8")
    assert "| 56,474 |" in figures  # 売上高 2026-03, million yen
    assert export.SOURCE_CREDIT in figures
