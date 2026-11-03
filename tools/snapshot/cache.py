"""Downloads kept on disk, so a rebuild or a resumed run makes no repeat calls.

    <work>/codelist/Edinetcode_<YYYY-MM-DD>.zip
    <work>/lists/<YYYY-MM-DD>.json
    <work>/csv/<docID>.zip
    <work>/unavailable/<docID>.txt   documents EDINET said it doesn't have
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from edinet_util_mcp_server import api


class Cache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def codelist(self, today: date) -> bytes:
        path = self._path("codelist", f"Edinetcode_{today.isoformat()}.zip")
        if not path.exists():
            _write(path, api.download_codelist())
        return path.read_bytes()

    def daily_list(self, client: api.EdinetClient, day: str, refresh: bool = False
                   ) -> dict[str, Any]:
        path = self._path("lists", f"{day}.json")
        if refresh or not path.exists():
            _write(path, json.dumps(client.daily_list(day), ensure_ascii=False).encode())
        return json.loads(path.read_text(encoding="utf-8"))

    def has_daily_list(self, day: str) -> bool:
        return self._path("lists", f"{day}.json").exists()

    def csv_zip(self, client: api.EdinetClient, doc_id: str) -> bytes:
        """The type-5 ZIP. Raises api.DocumentUnavailable, remembered across runs."""
        path = self._path("csv", f"{doc_id}.zip")
        if path.exists():
            return path.read_bytes()
        missing = self._path("unavailable", f"{doc_id}.txt")
        if missing.exists():
            raise api.DocumentUnavailable(missing.read_text(encoding="utf-8"))
        try:
            data = client.document(doc_id, api.DOC_TYPE_CSV)
        except api.DocumentUnavailable as exc:
            _write(missing, str(exc).encode())
            raise
        _write(path, data)
        return data

    def cached_csv(self, doc_id: str) -> bytes | None:
        """The type-5 ZIP if it is already on disk; never calls the API."""
        path = self._path("csv", f"{doc_id}.zip")
        return path.read_bytes() if path.exists() else None

    def has_csv(self, doc_id: str) -> bool:
        return (self._path("csv", f"{doc_id}.zip").exists()
                or self._path("unavailable", f"{doc_id}.txt").exists())


def _write(path: Path, data: bytes) -> None:
    # Whole files only: an interrupted run must not leave a truncated cache entry.
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)
