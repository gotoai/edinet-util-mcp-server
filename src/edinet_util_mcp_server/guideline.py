"""The guideline `load_guideline` returns: how the tools work together, and the
conditions of use. It is the one place for everything that is not specific to a
single tool, per Kari's skill-style MCP contract."""

from __future__ import annotations

from .export import SOURCE_CREDIT

SECTIONS: dict[str, str] = {
    "roles": """\
## このツールと、企業プロファイルのスキル

- このツールは EDINET API を直接呼びます。提出書類を日付で探す（list_filings）ことと、
  書類を Markdown に変換して読む（export_filing）ことができます。
- 会社の特定、5年分の主要な数値、同業候補、各社の書類管理番号は、スキル
  edinet-company-profiles（読み取り専用のデータベース）にあります。使えるときは先にスキルで
  書類管理番号を調べ、それを export_filing に渡してください。日付を総当たりする必要が
  なくなります。
- スキルのデータはスナップショット時点までです。それより新しい提出や、取り下げ・訂正の有無は
  list_filings で確かめます。""",
    "search": """\
## 書類の探し方（list_filings）

- EDINET は会社名や証券コードでは検索できず、提出日ごとの一覧しかありません。list_filings は
  日付の範囲（最大31日）の一覧を取り寄せて、会社や書類の種類で絞り込みます。1日あたり
  約1秒かかります。
- 範囲は狭く取ってください。有価証券報告書は決算日から3か月以内（3月決算なら6月中・下旬に
  集中）、半期報告書は半期末から45日以内に提出されます。
- 主な書類種別コード：120 有価証券報告書、130 その訂正、160 半期報告書、170 その訂正、
  180 臨時報告書、220 自己株券買付状況報告書、235 内部統制報告書、350 大量保有報告書。
- withdrawn が true の書類は取り下げられています。訂正報告書は parent_doc_id が元の書類です。
- 過去の日付の一覧も、取り下げや訂正を反映して毎晩作り直されます。当日の一覧は
  8時30分から随時更新されます。""",
    "read": """\
## 書類の読み方（export_filing）

- export_filing は書類を取得して、出力フォルダに Markdown として保存し、ファイルの一覧を
  返します。中身はファイルを開いて読んでください（同じファイルを利用者も読めます）。
  同じ書類をもう一度頼むと、保存済みのフォルダをそのまま返します（API は呼びません）。
- index.md に書類の情報（会社、会計基準、連結か単体か、当期・前期の期間）と目次があります。
  sections/ の各ファイルが有価証券報告書の項目（例：事業等のリスク）に当たります。
  全部は読まず、目次から必要な項目だけを開いてください。
- figures.md は5年分の主要な数値を表にしたものです（XBRL の CSV から再計算）。
- 表の金額の単位は表ごとに違います（千円、百万円など）。表の上の（単位：…）を必ず確かめて
  ください。行の先頭の全角スペースは勘定科目の階層です。
- XBRL のない書類（一部の外国会社など）は変換できません。そのときは返された URL から
  原本を開くよう案内してください。""",
    "terms": f"""\
## 利用条件（必ず守ってください）

- 出典表示：EDINET から取得した数値や文章を回答・資料に使うときは、次の出典と加工の表示を
  必ず添えます。
  - {SOURCE_CREDIT}
  - 「EDINET閲覧（提出）サイトの提出書類（書類管理番号 ○○）をもとに作成」と、加工したことを
    書きます。変換した Markdown や再計算した数値を、加工していない政府のデータであるかの
    ように示してはいけません。
- 書類の原本は、書類ごとの EDINET のページ（各ファイルの「原本」の URL）で確認できます。
  重要な判断の前には原本で確かめるよう、利用者に伝えてください。
- アクセス：同じ一覧や書類を短時間に繰り返し取得しないでください。リクエストは1件ずつ、
  約1秒の間隔で送られます。
- 本ツールは金融庁とは関係のない非公式のツールです。""",
}

HEADER = "# EDINET ツールの使い方\n"

# Always served whole: an agent that read one section would miss the conditions of use.
GUIDELINE = HEADER + "\n\n".join(SECTIONS.values())
