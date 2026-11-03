"""The inline-XBRL → Markdown converter, one test per fix from the 2026-11-02 agent test."""

from edinet_util_mcp_server import convert

IX = 'xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"'


def page(body: str) -> bytes:
    return f'<html {IX}><head><title>t</title></head><body>{body}</body></html>'.encode()


def cell(text: str, margin: float | None = None, **attrs: str) -> str:
    style = f' style="margin-left: {margin}px"' if margin is not None else ""
    extra = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f"<td{extra}><p{style}>{text}</p></td>"


def number(value: str) -> str:
    return f'<ix:nonFraction name="x" contextRef="c" unitRef="JPY">{value}</ix:nonFraction>'


def only(body: str) -> str:
    sections = convert.sections(page(body), "0105010")
    assert len(sections) == 1, [s.title for s in sections]
    return sections[0].markdown


def test_merged_number_cells_are_written_once_and_labels_repeated():
    md = only("<h3>１【配当】</h3><table>"
              f"<tr>{cell('決議')}{cell('種類')}{cell('総額')}</tr>"
              f"<tr>{cell('取締役会', rowspan='2')}{cell('普通株式')}"
              f"{cell(number('4,030'), rowspan='2')}</tr>"
              f"<tr>{cell('A種株式')}</tr></table>")
    rows = [line for line in md.splitlines() if line.startswith("| 取締役会")]
    assert rows == ["| 取締役会 | 普通株式 | 4,030 |", "| 取締役会 | A種株式 |  |"]


def test_unit_line_goes_above_the_table_and_the_period_row_is_the_header():
    md = only("<h3>１【貸借対照表】</h3><table>"
              f"<tr>{cell('&#160;')}{cell('&#160;')}{cell('(単位：千円)')}</tr>"
              f"<tr>{cell('&#160;')}{cell('前期')}{cell('当期')}</tr>"
              f"<tr>{cell('現金')}{cell(number('10'))}{cell(number('12'))}</tr></table>")
    lines = md.splitlines()
    unit = lines.index("(単位：千円)")
    assert lines[unit + 2] == "|  | 前期 | 当期 |"


def test_line_item_hierarchy_becomes_leading_full_width_spaces():
    md = only("<h3>１【貸借対照表】</h3><table>"
              f"<tr>{cell('')}{cell('当期')}</tr>"
              f"<tr>{cell('資産の部', 10)}{cell('')}</tr>"
              f"<tr>{cell('流動資産', 27.3)}{cell('')}</tr>"
              f"<tr>{cell('現金及び預金', 44.6)}{cell(number('5'))}</tr></table>")
    assert "| 資産の部 |  |" in md
    assert "| 　流動資産 |  |" in md
    assert "| 　　現金及び預金 | 5 |" in md


def test_numbered_paragraphs_become_headings():
    md = only("<h3>１【経営方針】</h3><p>(1）会社経営の基本方針</p><p>本文です。</p>"
              "<p>①基本の考え方</p><p>(2）これは見出しではない長い文章で、句点で終わっています。</p>")
    assert "## (1）会社経営の基本方針" in md
    assert "### ①基本の考え方" in md
    assert "## (2）これは" not in md


def test_footnote_marks_move_from_number_cells_to_the_label():
    md = only("<h3>１【貸借対照表】</h3><table>"
              f"<tr>{cell('')}{cell('当期')}</tr>"
              f"<tr>{cell('土地', 44.6)}{cell('※２,※３ ' + number('2,043'))}</tr></table>")
    assert "| 土地 ※２,※３ | 2,043 |" in md


def test_a_table_split_by_a_page_break_is_joined():
    header = f"<tr>{cell('')}{cell('前期')}{cell('当期')}</tr>"
    md = only("<h3>１【損益計算書】</h3>"
              f"<table>{header}<tr>{cell('売上高')}{cell(number('1'))}{cell(number('2'))}</tr></table>"
              f"<table>{header}<tr>{cell('売上原価')}{cell(number('3'))}{cell(number('4'))}</tr>"
              "</table>")
    assert md.count("| 前期 | 当期 |") == 1
    assert "| 売上原価 | 3 | 4 |" in md


def test_images_are_linked_from_the_section_files():
    sections = convert.sections(page('<h3>１【事業の内容】</h3><p>図の通り。</p>'
                                     '<img src="images/0101010_001.png"/>'), "0101010")
    assert "![図](../images/images/0101010_001.png)" not in sections[0].markdown
    assert "![図](../images/0101010_001.png)" in sections[0].markdown
    assert sections[0].images == ["images/0101010_001.png"]


def test_chapters_split_at_sub_sections_and_titles_come_from_the_brackets():
    sections = convert.sections(page(
        "<h2>第２【事業の状況】</h2><h3>１【経営方針】</h3><p>a</p><h3>２【事業等のリスク】</h3>"
        "<p>b</p>"), "0102010")
    assert [(s.index, s.title) for s in sections] == [(0, "経営方針"), (1, "事業等のリスク")]
    assert sections[1].markdown.startswith("# ２【事業等のリスク】")


def test_hidden_xbrl_header_is_skipped():
    md = only('<div style="display:none"><ix:header>隠れた値</ix:header></div>'
              "<h3>１【沿革】</h3><p>創業</p>")
    assert "隠れた値" not in md


def test_long_sub_sections_are_split_at_the_next_heading_level(monkeypatch):
    monkeypatch.setattr(convert, "MAX_SECTION_CHARS", 300)
    body = "<h3>１【連結財務諸表等】</h3>" + "".join(
        f"<h5>{n}【表{n}】</h5><p>{'あ' * 200}。</p>" for n in "①②③")
    sections = convert.sections(page(body), "0105010")
    assert [s.title for s in sections] == ["連結財務諸表等・表①", "連結財務諸表等（続き）・表②",
                                           "連結財務諸表等（続き）・表③"]
    assert sections[0].markdown.startswith("# １【連結財務諸表等】")
