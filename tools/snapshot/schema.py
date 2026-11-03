"""The profile snapshot's SQLite schema (design D8).

Amounts are whole yen as filed; ratios are fractions (0.101 = 10.1%), as in the CSV.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS manifest (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS sources (
  source TEXT PRIMARY KEY,   -- edinet_codelist, edinet_daily_lists, edinet_csv
  url    TEXT,
  as_of  TEXT,               -- ISO date; for daily lists, covered through
  note   TEXT
);
CREATE TABLE IF NOT EXISTS companies (
  edinet_code         TEXT PRIMARY KEY,
  sec_code            TEXT,     -- 5 chars, may be alphanumeric (e.g. 130A0)
  jcn                 TEXT,     -- 法人番号
  name                TEXT NOT NULL,
  name_en             TEXT,
  name_kana           TEXT,
  filer_type          TEXT,     -- 内国法人・組合 | 外国法人・組合
  industry            TEXT,     -- TSE 33-industry name, from the code list
  fiscal_year_end     TEXT,     -- MM-DD, from the code list
  capital_million_yen INTEGER,  -- from the code list
  address             TEXT,
  market              TEXT,     -- from the annual report, e.g. 東京証券取引所プライム市場
  market_as_of        TEXT,     -- submission date of that report
  accounting_standard TEXT,     -- Japan GAAP | IFRS | US GAAP | JMIS
  latest_annual_doc_id TEXT,
  history_years       INTEGER,  -- fiscal years in that report's 5-year table
  profile_status      TEXT      -- ok | short_history | no_annual_report | no_csv
                                -- | unavailable | parse_error; only 'ok' has
                                -- financials and peers
);
CREATE TABLE IF NOT EXISTS filings (
  doc_id         TEXT PRIMARY KEY,
  edinet_code    TEXT REFERENCES companies,
  doc_type_code  TEXT,      -- 120 annual report, 130 its amendment, 160 semi-annual, ...
  form_code      TEXT,
  ordinance_code TEXT,
  description    TEXT,      -- docDescription
  period_start   TEXT,
  period_end     TEXT,
  submitted_at   TEXT,      -- submitDateTime, JST
  parent_doc_id  TEXT,      -- for amendments: the amended document
  xbrl           INTEGER,
  csv            INTEGER,
  pdf            INTEGER,
  english        INTEGER,
  attachments    INTEGER
);
CREATE INDEX IF NOT EXISTS filings_by_company ON filings (edinet_code, submitted_at);
CREATE TABLE IF NOT EXISTS financials (
  edinet_code          TEXT REFERENCES companies,
  period_end           TEXT,     -- fiscal year end
  doc_id               TEXT,     -- the annual report it was read from
  standard             TEXT,
  basis                TEXT,     -- consolidated | non-consolidated
  currency             TEXT DEFAULT 'JPY',
  revenue_label        TEXT,     -- the filer's own top line: 売上高, 売上収益, 経常収益, ...
  revenue              INTEGER,
  operating_income     INTEGER,  -- not comparable across standards (D11)
  ordinary_income      INTEGER,  -- 経常利益, Japanese GAAP only
  profit_before_tax    INTEGER,
  net_income           INTEGER,  -- attributable to owners of the parent
  comprehensive_income INTEGER,
  depreciation         INTEGER,  -- from the cash-flow statement; latest two years only
  total_assets         INTEGER,
  net_assets           INTEGER,
  equity_ratio         REAL,
  roe                  REAL,
  eps                  REAL,
  bps                  REAL,
  per                  REAL,     -- NULL when loss-making
  dps                  REAL,
  payout_ratio         REAL,
  operating_cf         INTEGER,
  investing_cf         INTEGER,
  financing_cf         INTEGER,
  cash                 INTEGER,
  issued_shares        INTEGER,
  employees            INTEGER,  -- latest year only
  market_cap_fye       INTEGER,  -- PER × EPS × (issued − treasury); latest year only
  PRIMARY KEY (edinet_code, period_end)
);
CREATE TABLE IF NOT EXISTS peers (
  edinet_code   TEXT REFERENCES companies,
  peer_code     TEXT REFERENCES companies,
  rank          INTEGER,
  score         REAL,      -- 0.5 * scale similarity + 0.5 * business-text similarity
  reason        TEXT,
  peer_group_id INTEGER,
  PRIMARY KEY (edinet_code, peer_code)
);
"""

FINANCIAL_COLUMNS = (
    "revenue", "operating_income", "ordinary_income", "profit_before_tax", "net_income",
    "comprehensive_income", "depreciation", "total_assets", "net_assets", "equity_ratio",
    "roe", "eps", "bps", "per", "dps", "payout_ratio", "operating_cf", "investing_cf",
    "financing_cf", "cash", "issued_shares",
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA foreign_keys=OFF")
    db.executescript(SCHEMA)
    return db


def set_manifest(db: sqlite3.Connection, **values: str) -> None:
    db.executemany("INSERT OR REPLACE INTO manifest (key, value) VALUES (?, ?)",
                   list(values.items()))


def set_source(db: sqlite3.Connection, source: str, url: str, as_of: str | None,
               note: str) -> None:
    db.execute("INSERT OR REPLACE INTO sources (source, url, as_of, note) VALUES (?, ?, ?, ?)",
               (source, url, as_of, note))
