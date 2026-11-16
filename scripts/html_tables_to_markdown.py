#!/usr/bin/env python3
"""Convert MinerU HTML <table> blocks into Markdown pipe tables.

VS Code's built-in Markdown preview renders `$...$` inside normal Markdown
tables, but treats a raw HTML <table> block as opaque HTML and leaves the
math delimiters literal.  This converter flattens rowspan/colspan into a
rectangular grid so the table can be previewed as Markdown.

Usage:
    python scripts/html_tables_to_markdown.py input.md output.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup


def _cell_text(cell) -> str:
    text = cell.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def html_table_to_markdown(table) -> str:
    rows = table.find_all("tr")
    if not rows:
        return ""

    occupied: dict[tuple[int, int], str] = {}
    max_col = 0
    for r, tr in enumerate(rows):
        col = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            while (r, col) in occupied:
                col += 1
            text = _cell_text(cell)
            rowspan = max(1, int(cell.get("rowspan", 1) or 1))
            colspan = max(1, int(cell.get("colspan", 1) or 1))
            for dr in range(rowspan):
                for dc in range(colspan):
                    # Repeat vertical spans for readability; don't repeat
                    # horizontal spans because the header row already shows it.
                    occupied[(r + dr, col + dc)] = text if dc == 0 else ""
            col += colspan
            max_col = max(max_col, col)

    grid: list[list[str]] = []
    for r in range(len(rows)):
        row = [occupied.get((r, c), "") for c in range(max_col)]
        if any(row):
            grid.append(row)

    if not grid:
        return ""

    def fmt(cell: str) -> str:
        return cell.replace("|", r"\|").strip()

    header = grid[0]
    lines = [
        "| " + " | ".join(fmt(c) for c in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in grid[1:]:
        lines.append("| " + " | ".join(fmt(c) for c in row) + " |")
    return "\n".join(lines)


def convert_markdown(text: str) -> str:
    pattern = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)

    def repl(match: re.Match[str]) -> str:
        soup = BeautifulSoup(match.group(0), "html.parser")
        table = soup.find("table")
        if table is None:
            return match.group(0)
        converted = html_table_to_markdown(table)
        if not converted:
            return match.group(0)
        return "\n\n" + converted + "\n\n"

    return pattern.sub(repl, text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    text = args.input.read_text(encoding="utf-8")
    converted = convert_markdown(text)
    args.output.write_text(converted, encoding="utf-8")
    print(f"{args.input} -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
