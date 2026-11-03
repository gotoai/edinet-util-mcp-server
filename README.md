# edinet-util-mcp-server

An MCP server for the [EDINET](https://disclosure2.edinet-fsa.go.jp/) API v2 (金融庁), and the
builder of the company-profile snapshot used by the `edinet-company-profiles` skill.

EDINET API v2 を使って有価証券報告書などの開示書類を扱う MCP サーバーと、上場企業のプロフィール・
主要指標・同業他社リストをまとめたスナップショットのビルダーです。

> **Unofficial.** This project is not affiliated with or endorsed by the Financial Services
> Agency (金融庁), which operates EDINET.

**Status:** work in progress. The snapshot builder works; the MCP server is not built yet.

## Layout

| Path | What it is |
|---|---|
| `src/edinet_util_mcp_server/` | The package: API client (`api`), code list (`codelist`), key figures from the type-5 CSV (`figures`) |
| `tools/build_profiles.py`, `tools/snapshot/` | The snapshot builder, a development tool not shipped in the wheel |
| `skill/edinet-company-profiles/` | The skill; the builder writes its database to `data/edinet_profiles.sqlite` (not in git) |

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
