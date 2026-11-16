#!/usr/bin/env python3
"""Convert a Markdown file into a standalone HTML page with MathJax.

This is a deterministic, zero-token converter.  It is intended for knowledge
cards, audit reports and MinerU Markdown that contain tables, code fences and
LaTeX such as ``$...$`` / ``$$...$$``.  MathJax runs in the browser and also
processes math inside raw HTML tables, which VS Code's built-in preview does
not.

Usage:
    python scripts/markdown_to_html.py card.md
    python scripts/markdown_to_html.py card.md card.html --title "知识卡"
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

import markdown

DEFAULT_MATHJAX_URL = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"

_CSS = """
:root { color-scheme: light dark; }
body {
  max-width: 980px; margin: 2rem auto; padding: 0 1.2rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC",
               "Microsoft YaHei", sans-serif;
  line-height: 1.75; color: #1f2328; background: #fff;
}
h1, h2, h3, h4, h5, h6 { line-height: 1.3; margin-top: 1.4em; }
h1 { border-bottom: 1px solid #d8dee4; padding-bottom: .3rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: .92rem; }
th, td { border: 1px solid #d0d7de; padding: .35rem .55rem; vertical-align: top; }
thead th { background: #f6f8fa; }
tr:nth-child(even) td { background: #fbfcfd; }
code { background: #f6f8fa; padding: .1em .3em; border-radius: 4px; font-size: .9em; }
pre { background: #f6f8fa; padding: .85rem; overflow: auto; border-radius: 6px; }
pre code { background: transparent; padding: 0; }
blockquote { border-left: 4px solid #d0d7de; margin-left: 0; padding-left: 1rem; color: #57606a; }
img { max-width: 100%; height: auto; }
hr { border: 0; border-top: 1px solid #d8dee4; margin: 2rem 0; }
@media (prefers-color-scheme: dark) {
  body { color: #e6edf3; background: #0d1117; }
  th, td { border-color: #30363d; }
  thead th { background: #161b22; }
  tr:nth-child(even) td { background: #0f141a; }
  code, pre { background: #161b22; }
  blockquote { border-color: #30363d; color: #8b949e; }
  h1, hr { border-color: #30363d; }
}
"""

_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style>
__MATHJAX__
</head>
<body>
__BODY__
</body>
</html>
"""

_MATHJAX = r"""<script>
window.MathJax = {
  tex: {
    inlineMath: [['$', '$'], ['\\(', '\\)']],
    displayMath: [['$$', '$$'], ['\\[', '\\]']],
    processEscapes: true
  },
  options: {
    skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
  }
};
</script>
<script defer src="__URL__"></script>"""


def _page_title(markdown_text: str, fallback: str) -> str:
    match = re.search(r"^\s*#\s+(.+?)\s*$", markdown_text, re.MULTILINE)
    return match.group(1).strip() if match else fallback


def convert_markdown(
    markdown_text: str,
    *,
    title: str = "",
    mathjax_url: str = DEFAULT_MATHJAX_URL,
    mathjax: bool = True,
) -> str:
    body = markdown.markdown(
        markdown_text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        output_format="html5",
    )
    page_title = title.strip() or _page_title(markdown_text, "Markdown")
    mathjax_block = ""
    if mathjax:
        mathjax_block = _MATHJAX.replace("__URL__", html.escape(mathjax_url, quote=True))
    return (
        _TEMPLATE.replace("__TITLE__", html.escape(page_title))
        .replace("__CSS__", _CSS)
        .replace("__MATHJAX__", mathjax_block)
        .replace("__BODY__", body)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="input .md file")
    parser.add_argument("output", nargs="?", type=Path, help="output .html (default: same stem)")
    parser.add_argument("--title", default="", help="HTML title; default from first # heading")
    parser.add_argument("--mathjax-url", default=DEFAULT_MATHJAX_URL)
    parser.add_argument("--no-mathjax", action="store_true", help="omit MathJax")
    args = parser.parse_args()

    source = args.input
    if not source.is_file():
        print(f"not found: {source}", file=sys.stderr)
        return 1
    output = args.output or source.with_suffix(".html")

    text = source.read_text(encoding="utf-8")
    rendered = convert_markdown(
        text,
        title=args.title,
        mathjax_url=args.mathjax_url,
        mathjax=not args.no_mathjax,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
