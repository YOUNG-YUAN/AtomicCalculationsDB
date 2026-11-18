"""HTML table parsing for MinerU and other structured sources.

The parser keeps two views of one table:

* a rectangular cell grid with merged-cell metadata for programmatic use;
* a flattened Markdown pipe table for display and the existing radar paths.

Markdown cannot represent ``rowspan`` / ``colspan``; the grid remains the
canonical structured view, while the Markdown view repeats vertical spans.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

_INCOMPLETE_NUMERIC_RE = re.compile(
    r"^[+-]?\d+\.$|^[+-]?\d+(?:\.\d*)?[eE][+-]?$"
)


def is_truncated_table_row(row: list[str], max_cols: int | None = None) -> bool:
    """Detect whether a table row is truncated/incomplete (e.g. from OCR page edge crop)."""
    if not row:
        return False
    populated = [(i, c.strip()) for i, c in enumerate(row) if c and c.strip()]
    if not populated:
        return False
    last_idx, last_val = populated[-1]
    cols = max_cols if max_cols is not None else len(row)

    # 1. Incomplete numeric token in non-first column (e.g. '1.', '1.2E+', '-3.')
    if last_idx > 0 and _INCOMPLETE_NUMERIC_RE.match(last_val):
        return True

    # 2. Unclosed math delimiters with trailing empty cells
    unescaped = re.sub(r"\\\$", "", last_val)
    if unescaped.count("$") % 2 != 0 and last_idx < cols - 1:
        return True

    # 3. Severe column loss (>= 50% empty cells) with cut-off punctuation
    if cols >= 4 and len(populated) <= cols // 2:
        if last_val.endswith((".", "-", "...", "…", ",")) and not (
            last_idx == 0 and re.match(r"^\d+\.$", last_val)
        ):
            return True

    return False


def is_truncated_markdown_table_line(
    line: str, expected_cols: int | None = None
) -> bool:
    """Check if a single pipe table markdown line represents a truncated row."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    inner = stripped.strip("|")
    cells = [c.strip() for c in inner.split("|")]
    return is_truncated_table_row(cells, expected_cols or len(cells))


def sanitize_markdown_table_text(text: str) -> str:
    """Sanitize all markdown pipe tables within a text block, stripping truncated boundary rows."""
    if not text or "|" not in text:
        return text
    lines = text.splitlines(keepends=True)
    out_lines: list[str] = []
    table_buffer: list[str] = []

    def flush_table():
        nonlocal table_buffer
        if not table_buffer:
            return
        expected_cols = None
        for tline in table_buffer:
            s = tline.strip()
            if s.startswith("|") and not all(c in "| -:" for c in s):
                cells = [c.strip() for c in s.strip("|").split("|")]
                expected_cols = len(cells)
                break
        while table_buffer:
            last_line = table_buffer[-1].strip()
            if last_line.startswith("|") and is_truncated_markdown_table_line(
                last_line, expected_cols
            ):
                table_buffer.pop()
            else:
                break
        out_lines.extend(table_buffer)
        table_buffer = []

    for line in lines:
        if line.strip().startswith("|"):
            table_buffer.append(line)
        else:
            if table_buffer:
                flush_table()
            out_lines.append(line)
    if table_buffer:
        flush_table()
    return "".join(out_lines)


def _cell_text(cell: Any) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()


def _grid(table: Any) -> tuple[list[list[str]], list[list[dict[str, Any]]]]:
    rows = table.find_all("tr")
    occupied: dict[tuple[int, int], str] = {}
    cells: list[list[dict[str, Any]]] = []
    max_col = 0
    for row_index, tr in enumerate(rows):
        col = 0
        row_cells: list[dict[str, Any]] = []
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (row_index, col) in occupied:
                col += 1
            text = _cell_text(cell)
            rowspan = max(1, int(cell.get("rowspan", 1) or 1))
            colspan = max(1, int(cell.get("colspan", 1) or 1))
            row_cells.append(
                {
                    "row": row_index,
                    "col": col,
                    "text": text,
                    "header": cell.name.lower() == "th",
                    "rowspan": rowspan,
                    "colspan": colspan,
                }
            )
            for dr in range(rowspan):
                for dc in range(colspan):
                    # Repeat vertical spans for the Markdown view; horizontal
                    # spans are represented by the first column only.
                    occupied[(row_index + dr, col + dc)] = text if dc == 0 else ""
            col += colspan
            max_col = max(max_col, col)
        cells.append(row_cells)
    grid = [
        [occupied.get((row, col), "") for col in range(max_col)]
        for row in range(len(rows))
    ]
    # Prune trailing rows that were truncated by OCR page boundary crop
    while grid and is_truncated_table_row(grid[-1], max_col):
        grid.pop()
        if cells and len(cells) > len(grid):
            cells.pop()
    return grid, cells


def html_table_cells(html: str) -> list[list[dict[str, Any]]]:
    soup = BeautifulSoup(str(html or ""), "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    _grid_view, cells = _grid(table)
    return cells


def markdown_table_cells(markdown: str) -> list[list[dict[str, Any]]]:
    """Parse a pipe table into the same cell metadata shape as HTML tables."""
    lines = [
        line.strip()
        for line in str(markdown or "").splitlines()
        if line.strip().startswith("|")
    ]
    rows: list[list[dict[str, Any]]] = []
    for line_index, line in enumerate(lines):
        body = line.strip().strip("|")
        cells = [cell.strip().replace(r"\|", "|") for cell in body.split("|")]
        if cells and all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        row: list[dict[str, Any]] = []
        for col, text in enumerate(cells):
            row.append(
                {
                    "row": len(rows),
                    "col": col,
                    "text": text,
                    "header": line_index == 0,
                    "rowspan": 1,
                    "colspan": 1,
                    "bbox": None,
                }
            )
        rows.append(row)
    return rows


def table_header_rows(cells: list[list[dict[str, Any]]]) -> int:
    """Count leading rows whose non-empty cells are all marked as headers."""
    count = 0
    for row in cells:
        populated = [cell for cell in row if str(cell.get("text") or "").strip()]
        if not populated or not all(cell.get("header") for cell in populated):
            break
        count += 1
    return count


def html_table_to_markdown(html: str) -> str:
    soup = BeautifulSoup(str(html or ""), "html.parser")
    table = soup.find("table")
    if table is None:
        return ""
    grid, _cells = _grid(table)
    grid = [row for row in grid if any(cell for cell in row)]
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]

    def fmt(value: str) -> str:
        return value.replace("|", r"\|").strip()

    lines = [
        "| " + " | ".join(fmt(cell) for cell in grid[0]) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in grid[1:]:
        lines.append("| " + " | ".join(fmt(cell) for cell in row) + " |")
    return "\n".join(lines)


__all__ = [
    "html_table_cells",
    "html_table_to_markdown",
    "is_truncated_markdown_table_line",
    "is_truncated_table_row",
    "markdown_table_cells",
    "sanitize_markdown_table_text",
    "table_header_rows",
]
