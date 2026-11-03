"""An EDINET filing's inline-XBRL chapters → Markdown sections (design D4).

Each chapter file (0101010 企業の概況, 0102010 事業の状況, …) is split at its
sub-sections (the h3 headings, e.g. 「１【経営方針、経営環境及び対処すべき課題等】」),
one Markdown text per sub-section. What the 2026-11-02 agent test showed was needed:

1. Merged cells repeat their text only in label cells, never in number cells, so a
   column sum does not double-count.
2. The unit line 「(単位：千円)」 goes above the table, and the period row is the
   header row.
3. Line-item hierarchy, which the HTML draws with margin-left, is kept as leading
   full-width spaces in the label.
4. Paragraphs that work as headings — (1), ①, ａ．, 【…】 — become headings.
5. Footnote marks in number cells (※２,※３ 38,194,139) move to the row label.
6. Tables split by a page break (the same header twice in a row) are joined.
7. (Front matter: written by the exporter.)
8. Images are linked, not replaced by a placeholder.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from lxml import etree

SKIP = {"header", "script", "style", "head", "title"}  # ix:header holds hidden facts
HEADINGS = {f"h{n}": n for n in range(1, 7)}
BLOCKS = {"p", "div", "li", "ul", "ol", "body", "html", "section", "center", "blockquote"}
SECTION_LEVEL = 3
MAX_SECTION_CHARS = 30_000
INDENT = "　"

_SPACES = re.compile(r"[ \t\r\n\f\v ]+")
_MARGIN = re.compile(r"margin-left\s*:\s*(-?[\d.]+)px")
_UNIT = re.compile(r"^[(（]\s*単位\s*[:：][^)）]*[)）]$")
_NUMBER = re.compile(r"^[△▲\-−－]?\s*[\d,，.]+\s*[%％]?$|^[－—\-−]$")
_FOOTNOTE = re.compile(r"^((?:※\s*[\d０-９]+\s*[,，、]?\s*)+)\s*(.+)$")
_PROMOTE = (
    (re.compile(r"^\S{0,4}【[^】]{1,40}】$"), 0),
    (re.compile(r"^[(（]\s*\d+\s*[)）]"), 0),
    (re.compile(r"^[①-⑳]"), 1),
    (re.compile(r"^(?:[a-zA-Z]\s*[.)]|\([a-z]\))"), 2),
)
_PROMOTE_MAX_CHARS = 50


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Paragraph:
    text: str


@dataclass
class Image:
    src: str


@dataclass
class Table:
    rows: list[list[str]]
    units: list[str] = field(default_factory=list)


Block = Heading | Paragraph | Image | Table


@dataclass
class Section:
    chapter: str  # the chapter file's number, e.g. 0102010
    index: int  # 0 = text before the first sub-section
    title: str
    markdown: str
    images: list[str]


def _local(el: etree._Element) -> str:
    tag = el.tag
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _hidden(el: etree._Element) -> bool:
    return "display:none" in (el.get("style") or "").replace(" ", "").lower()


def _clean_lines(text: str) -> str:
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _text(el: etree._Element) -> str:
    """An element's text; block children and <br> start new lines."""
    parts: list[str] = []

    def walk(node: etree._Element) -> None:
        name = _local(node)
        if name in SKIP or (name and _hidden(node)):
            return
        if name == "br":
            parts.append("\n")
        elif name == "img":
            parts.append(f"![図]({node.get('src') or ''})")
        else:
            if name in BLOCKS:
                parts.append("\n")
            if node.text:
                parts.append(node.text)
            for child in node:
                walk(child)
                if child.tail:
                    parts.append(child.tail)
            if name in BLOCKS:
                parts.append("\n")

    walk(el)
    return _clean_lines("".join(parts))


def blocks(root: etree._Element) -> list[Block]:
    out: list[Block] = []
    buffer: list[str] = []

    def flush() -> None:
        text = _clean_lines("".join(buffer))
        buffer.clear()
        if text:
            out.append(Paragraph(text))

    def walk(node: etree._Element) -> None:
        name = _local(node)
        if not name or name in SKIP or _hidden(node):
            return
        if name in HEADINGS:
            flush()
            text = _text(node).replace("\n", " ")
            if text:
                out.append(Heading(HEADINGS[name], text))
        elif name == "table":
            flush()
            table = _table(node)
            if table:
                out.append(table)
        elif name == "img":
            flush()
            out.append(Image(node.get("src") or ""))
        elif name == "br":
            buffer.append("\n")
        else:
            block = name in BLOCKS
            if block:
                flush()
            if node.text:
                buffer.append(node.text)
            for child in node:
                walk(child)
                if child.tail:
                    buffer.append(child.tail)
            if block:
                flush()

    walk(root)
    flush()
    return out


@dataclass
class _Cell:
    text: str
    numeric: bool
    margin: float | None


def _cell(td: etree._Element) -> _Cell:
    text = _text(td)
    numeric = any(_local(el) == "nonfraction" for el in td.iter()) or bool(
        _NUMBER.match(unicodedata.normalize("NFKC", text)))
    margin = None
    for el in td.iter():
        match = _MARGIN.search(el.get("style") or "") if isinstance(el.tag, str) else None
        if match:
            margin = float(match.group(1))
            break
    return _Cell(text, numeric, margin)


def _table(table: etree._Element) -> Table | Paragraph | None:
    grid: list[list[_Cell | None]] = []
    pending: dict[tuple[int, int], _Cell] = {}
    rows = [tr for tr in table.iter() if _local(tr) == "tr"]
    for r, tr in enumerate(rows):
        row: list[_Cell | None] = []
        column = 0
        for td in (td for td in tr if _local(td) in ("td", "th")):
            while (r, column) in pending:
                row.append(pending.pop((r, column)))
                column += 1
            cell = _cell(td)
            rowspan = max(1, int(td.get("rowspan") or 1))
            colspan = max(1, int(td.get("colspan") or 1))
            # Fix 1: a merged number is written once; a merged label is repeated.
            copy = None if cell.numeric else _Cell(cell.text, False, cell.margin)
            for dc in range(colspan):
                row.append(cell if dc == 0 else copy)
                for dr in range(1, rowspan):
                    pending[(r + dr, column + dc)] = copy or _Cell("", False, None)
            column += colspan
        while (r, column) in pending:
            row.append(pending.pop((r, column)))
            column += 1
        grid.append(row)

    units: list[str] = []
    width = max((len(row) for row in grid), default=0)
    cells = [[c or _Cell("", False, None) for c in row] + [_Cell("", False, None)]
             * (width - len(row)) for row in grid]
    for row in cells:
        for cell in row:
            if _UNIT.match(cell.text.replace("\n", "")):
                if cell.text not in units:
                    units.append(cell.text.replace("\n", ""))
                cell.text = ""

    # Fix 3: indentation levels from the label column's distinct margins.
    margins = sorted({round(c[0].margin) for c in cells if c and c[0].margin is not None
                      and not c[0].numeric})
    level = {m: i for i, m in enumerate(margins)}

    text_rows: list[list[str]] = []
    for row in cells:
        texts = [c.text.replace("\n", "<br>") for c in row]
        if row and row[0].margin is not None and not row[0].numeric and texts[0]:
            texts[0] = INDENT * level.get(round(row[0].margin), 0) + texts[0]
        # Fix 5: footnote marks move from number cells to the label.
        for i, cell in enumerate(row[1:], 1):
            match = _FOOTNOTE.match(cell.text)
            if match and _NUMBER.match(unicodedata.normalize("NFKC", match.group(2))):
                texts[i] = match.group(2)
                marks = re.sub(r"\s+", "", match.group(1)).rstrip(",，、")
                if marks not in texts[0]:
                    texts[0] = f"{texts[0]} {marks}" if texts[0] else marks
        text_rows.append(texts)

    text_rows = [row for row in text_rows if any(row)]
    if not text_rows:
        return Paragraph(" ".join(units)) if units else None
    keep = [c for c in range(width) if any(row[c] for row in text_rows)]
    text_rows = [[row[c] for c in keep] for row in text_rows]
    # Columns made by colspan carry nothing their left neighbour does not.
    columns = [0] + [c for c in range(1, len(keep))
                     if any(row[c] and row[c] != row[c - 1] for row in text_rows)]
    text_rows = [[row[c] for c in columns] for row in text_rows]
    if len(columns) == 1:
        lines = [row[0].replace("<br>", "\n") for row in text_rows]
        return Paragraph("\n\n".join([*units, *lines]))
    text_rows[0] = [cell.replace("<br>", " ") for cell in text_rows[0]]
    return Table(text_rows, units)


def _promoted(block: Block, base: int) -> Block:
    if not isinstance(block, Paragraph) or "\n" in block.text:
        return block
    # NFKC for full-width letters and digits, the raw text for ①, which NFKC makes "1".
    text = unicodedata.normalize("NFKC", block.text)
    if len(text) > _PROMOTE_MAX_CHARS or text.endswith("。"):
        return block
    for pattern, rank in _PROMOTE:
        if pattern.match(text) or pattern.match(block.text):
            return Heading(min(6, base + 1 + rank), block.text)
    return block


def _joined(items: list[Block]) -> list[Block]:
    """Fix 6: a table continued after a page break repeats its header; join them."""
    out: list[Block] = []
    for block in items:
        previous = out[-1] if out else None
        if (isinstance(block, Table) and isinstance(previous, Table)
                and block.rows[0] == previous.rows[0]):
            previous.rows.extend(block.rows[1:])
            continue
        out.append(block)
    return out


def _render(block: Block, top: int, image_prefix: str) -> str:
    if isinstance(block, Heading):
        return "#" * max(1, min(6, block.level - top + 1)) + " " + block.text
    if isinstance(block, Image):
        return f"![図]({image_prefix}{block.src})"
    if isinstance(block, Table):
        def line(row: list[str]) -> str:
            return "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
        lines = [*block.units, ""] if block.units else []
        lines += [line(block.rows[0]), "|" + "---|" * len(block.rows[0])]
        lines += [line(row) for row in block.rows[1:]]
        return "\n".join(lines)
    return re.sub(r"!\[図\]\((?!https?:)", f"![図]({image_prefix}", block.text)


def _title(heading: str) -> str:
    match = re.search(r"【([^】]+)】", heading)
    return (match.group(1) if match else heading).strip()


def sections(html: bytes, chapter: str, *, image_prefix: str = "../") -> list[Section]:
    """One chapter file → its sub-sections as Markdown."""
    parser = etree.XMLParser(huge_tree=True, recover=True)
    root = etree.fromstring(html, parser)
    body = next((el for el in root.iter() if _local(el) == "body"), root)
    items = blocks(body)

    base = SECTION_LEVEL
    promoted: list[Block] = []
    for block in items:
        if isinstance(block, Heading):
            base = block.level
        else:
            block = _promoted(block, base)
        promoted.append(block)
    items = _joined(promoted)

    has_sections = any(isinstance(b, Heading) and b.level == SECTION_LEVEL for b in items)
    groups: list[list[Block]] = [[]]
    for block in items:
        if (has_sections and isinstance(block, Heading) and block.level <= SECTION_LEVEL
                and groups[-1]):
            groups.append([])
        groups[-1].append(block)

    out: list[Section] = []
    for group in groups:
        pieces = [p for p in _split(group, image_prefix)
                  if not all(isinstance(b, Heading) for b in p)]
        whole = _piece_title(group, chapter)
        for number, piece in enumerate(pieces):
            headings = [b for b in piece if isinstance(b, Heading)]
            top = min((h.level for h in headings), default=SECTION_LEVEL)
            markdown = "\n\n".join(_render(b, top, image_prefix) for b in piece) + "\n"
            images = [b.src for b in piece if isinstance(b, Image)]
            images += [m for b in piece if isinstance(b, Paragraph)
                       for m in re.findall(r"!\[図\]\(([^)]+)\)", b.text)]
            title = _piece_title(piece, chapter)
            if len(pieces) > 1 and title != whole:
                title = f"{whole}{'（続き）' if number else ''}・{title}"
            out.append(Section(chapter, len(out), title, markdown, images))
    return out


def _size(piece: list[Block], image_prefix: str) -> int:
    return sum(len(_render(b, 1, image_prefix)) for b in piece)


def _split(group: list[Block], image_prefix: str) -> list[list[Block]]:
    """A sub-section over MAX_SECTION_CHARS, split at its next heading level and
    packed back into pieces of up to that size.

    A piece holding only headings is carried into the next piece, so each file
    starts with the headings that lead to its text.
    """
    if _size(group, image_prefix) <= MAX_SECTION_CHARS:
        return [group]
    for level in sorted({b.level for b in group[1:] if isinstance(b, Heading)}):
        parts: list[list[Block]] = [[]]
        for block in group:
            if (isinstance(block, Heading) and block.level == level and parts[-1]
                    and not all(isinstance(b, Heading) for b in parts[-1])):
                parts.append([])
            parts[-1].append(block)
        if len(parts) > 1:
            pieces = [piece for part in parts for piece in _split(part, image_prefix)]
            return _pack(pieces, image_prefix)
    return [group]


def _pack(pieces: list[list[Block]], image_prefix: str) -> list[list[Block]]:
    out: list[list[Block]] = []
    size = 0
    for piece in pieces:
        piece_size = _size(piece, image_prefix)
        if out and size + piece_size <= MAX_SECTION_CHARS:
            out[-1] = out[-1] + piece
            size += piece_size
        else:
            out.append(list(piece))
            size = piece_size
    return out


def _piece_title(piece: list[Block], chapter: str) -> str:
    """The deepest heading the piece starts with that is the only one of its level
    in the piece: the subject of the whole piece, not of its first part."""
    headings = [b for b in piece if isinstance(b, Heading)]
    leading = []
    for block in piece:
        if not isinstance(block, Heading):
            break
        leading.append(block)
    for heading in reversed(leading):
        if sum(h.level == heading.level for h in headings) == 1:
            return _title(heading.text)
    return _title(headings[0].text) if headings else chapter
