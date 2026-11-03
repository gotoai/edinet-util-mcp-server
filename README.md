# edinet-util-mcp-server

An MCP server for the [EDINET](https://disclosure2.edinet-fsa.go.jp/) API v2 (金融庁), and the
builder of the company-profile snapshot used by the `edinet-company-profiles` skill.

EDINET API v2 を使って有価証券報告書などの開示書類を扱う MCP サーバーと、上場企業のプロフィール・
主要指標・同業他社リストをまとめたスナップショットのビルダーです。

> **Unofficial.** This project is not affiliated with or endorsed by the Financial Services
> Agency (金融庁), which operates EDINET.

**Status:** work in progress (0.1.0.dev0). The server, the skill and the snapshot builder work;
not yet published.

## Layout

| Path | What it is |
|---|---|
| `src/edinet_util_mcp_server/` | The package: the MCP server (`server`, `guideline`), filing export (`export`, `convert`), API client (`api`), code list (`codelist`), key figures from the type-5 CSV (`figures`) |
| `tools/build_profiles.py`, `tools/snapshot/` | The snapshot builder, a development tool not shipped in the wheel |
| `skill/edinet-company-profiles/` | The skill: `SKILL.md`, `scripts/edinet_profiles.py` (standard library only), `references/financial_diagnosis.md`, `NOTICE`; the builder writes its database to `data/edinet_profiles.sqlite` (not in git) |

## The server

A skill-style MCP server over stdio, with the user's own API key:

| Tool | Does |
|---|---|
| `load_guideline` | How the tools work together, the conditions of use |
| `list_filings` | Filings submitted between two dates (at most 31 days), filtered by company or document type |
| `export_filing` | One filing → Markdown files: `index.md`, `figures.md`, `sections/`, `images/` |

Settings (environment variables): `EDINET_API_KEY` (required) and `EDINET_OUTPUT_DIR`
(default `~/Documents/EDINET`), the only place the server writes. Run it with
`python -m edinet_util_mcp_server`; Kari finds the command through the `kari.mcp_servers`
entry point.

## The skill

`skill/edinet-company-profiles/` is a standard `SKILL.md` folder. Copy it, with its database,
into `~/.agents/skills/` (Kari's main app) or import it in an Agent Desk. Its script opens the
database read-only in place:

```sh
python skill/edinet-company-profiles/scripts/edinet_profiles.py compare 7203 --peers "7267,7201"
```

Commands: `company`, `peers`, `compare`, `filings`, `sql`, `about` (`--json` for structured
output).

## Tests

```sh
../../.venv/bin/pytest            # offline
EDINET_LIVE=1 ../../.venv/bin/pytest tests/test_live.py   # real API over stdio, key from .env
../../.venv/bin/ruff check src tests tools skill
```

## Building the snapshot

Put the key in `.env` at the repository root (`EDINET_API_KEY=…`; the file is ignored by git),
then:

```sh
python tools/build_profiles.py all
```

Steps, each re-runnable on its own: `cohort` (every listed company in the EDINET code list) →
`filings` (every cohort filing in the daily lists of the last 18 months, or `--from`..`--to`)
→ `profiles` (key figures and listing market from each latest annual report's CSV; a company
is covered when that report shows at least 3 fiscal years) → `peers` (top 10 per company in
the same TSE industry, scored 0.5 × scale similarity + 0.5 × similarity of the report's
事業の内容 and 経営方針 text) → `finish` (manifest, attribution, `VACUUM`).

The builder needs the dev extras (`pip install -e ".[dev]"`, which include numpy and
scikit-learn for the `peers` step); the server itself does not.

Downloads are cached in `build/edinet-cache/`, so an interrupted run resumes and a rebuild makes
no repeat calls. Requests go one at a time, 1 second apart, slowing down after an HTTP 429.

## Data and licence

The code is MIT-licensed. Data from EDINET is used under the
[公共データ利用規約（第1.0版）](https://www.digital.go.jp/resources/open_data/public_data_license_v1.0)
(PDL1.0). The snapshot is processed data, not the original filings:

- 出典：EDINET閲覧（提出）サイト（https://disclosure2.edinet-fsa.go.jp/）、PDL1.0（https://www.digital.go.jp/resources/open_data/public_data_license_v1.0）
- EDINET閲覧（提出）サイトの提出書類をもとに GotoAI が加工して作成
