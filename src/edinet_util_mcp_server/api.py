"""The EDINET API v2, as this package uses it.

Two endpoints, both GETs with the key in the `Subscription-Key` parameter:
- `GET /documents.json?date=YYYY-MM-DD&type=2` lists every filing made that day.
  There is no search by company or keyword.
- `GET /documents/{docID}?type=N` downloads one filing: 1 = body and XBRL (ZIP),
  2 = PDF, 3 = attachments (ZIP), 4 = English (ZIP), 5 = XBRL converted to CSV (ZIP).

Errors often come back as JSON with HTTP 200, so a download is checked by its
Content-Type, not only its status.

There is no published rate limit, but the terms forbid heavy access in a short
time. Requests go one at a time, 1 second apart, widening to 2 seconds after a
429 or server error, with exponential back-off while 429s repeat (design D16).

Source: EDINET API仕様書 Version 2 (updated 2026-06-03).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import httpx

BASE_URL = "https://api.edinet-fsa.go.jp/api/v2"
CODELIST_URL = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
DOCUMENT_PAGE_URL = "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{doc_id}"

DOC_TYPE_ZIP = 1
DOC_TYPE_PDF = 2
DOC_TYPE_ATTACHMENTS = 3
DOC_TYPE_ENGLISH = 4
DOC_TYPE_CSV = 5

NORMAL_SPACING = 1.0
WIDE_SPACING = 2.0
MAX_BACKOFF = 120.0
MAX_ATTEMPTS = 6


class EdinetError(RuntimeError):
    """The API answered, but not with what was asked for."""


class DocumentUnavailable(EdinetError):
    """The document is not (or no longer) available, e.g. withdrawn or not disclosed."""


def load_api_key(env_file: Path | None = None) -> str:
    """`EDINET_API_KEY` from the environment, else from a dotenv-style file.

    The key is never printed or logged.
    """
    key = os.environ.get("EDINET_API_KEY", "").strip()
    if key:
        return key
    if env_file and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == "EDINET_API_KEY":
                key = value.strip().strip("'\"")
                if key:
                    return key
    raise EdinetError("EDINET_API_KEY is not set.")


@dataclass
class _Pacer:
    spacing: float = NORMAL_SPACING
    last: float = 0.0

    def wait(self) -> None:
        delay = self.last + self.spacing - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()


class EdinetClient:
    """A paced, sequential client. Not thread-safe by design: one request at a time."""

    def __init__(self, api_key: str, *, timeout: float = 120.0,
                 transport: httpx.BaseTransport | None = None,
                 spacing: float = NORMAL_SPACING) -> None:
        self._key = api_key
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"User-Agent": "edinet-util-mcp-server"})
        self._pacer = _Pacer(spacing=spacing)
        self.requests = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        params = {**params, "Subscription-Key": self._key}
        backoff = WIDE_SPACING
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._pacer.wait()
            self.requests += 1
            try:
                response = self._http.get(f"{BASE_URL}{path}", params=params)
            except httpx.TransportError:
                if attempt == MAX_ATTEMPTS:
                    raise
                self._pacer.spacing = WIDE_SPACING
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                self._pacer.spacing = WIDE_SPACING
                if attempt == MAX_ATTEMPTS:
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue
            return response
        raise EdinetError(f"EDINET kept refusing {path} (HTTP {response.status_code}).")

    def daily_list(self, date: str) -> dict[str, Any]:
        """The filing list for one day (`type=2`, with document details)."""
        response = self._get("/documents.json", {"date": date, "type": 2})
        data = _json_or_error(response)
        status = str(data.get("metadata", {}).get("status", ""))
        if status != "200":
            raise EdinetError(f"Daily list {date}: {_error_message(data)}")
        return data

    def document(self, doc_id: str, doc_type: int) -> bytes:
        """One document's file. Raises DocumentUnavailable when EDINET has none."""
        response = self._get(f"/documents/{doc_id}", {"type": doc_type})
        content_type = response.headers.get("Content-Type", "")
        if response.status_code == 200 and "json" not in content_type:
            return response.content
        data = _json_or_error(response)
        status = str(data.get("metadata", {}).get("status") or data.get("StatusCode") or "")
        message = _error_message(data)
        if status == "404" or response.status_code == 404:
            raise DocumentUnavailable(f"{doc_id} type {doc_type}: {message}")
        raise EdinetError(f"{doc_id} type {doc_type}: {message}")


def download_codelist(timeout: float = 120.0) -> bytes:
    """The EDINET code list ZIP. Public, no key, updated daily."""
    response = httpx.get(CODELIST_URL, timeout=timeout,
                         headers={"User-Agent": "edinet-util-mcp-server"})
    response.raise_for_status()
    if not response.content.startswith(b"PK"):
        raise EdinetError("The EDINET code list download is not a ZIP file.")
    return response.content


def _json_or_error(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EdinetError(f"Unexpected answer (HTTP {response.status_code}, "
                          f"{response.headers.get('Content-Type', '')}).") from exc
    if not isinstance(data, dict):
        raise EdinetError(f"Unexpected answer (HTTP {response.status_code}).")
    return data


def _error_message(data: dict[str, Any]) -> str:
    metadata = data.get("metadata", {})
    message = metadata.get("message") or data.get("message") or "unknown error"
    status = metadata.get("status") or data.get("StatusCode") or ""
    return f"{status} {message}".strip()
