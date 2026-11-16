"""Markdown + atomic-physics notation rendering for Qt rich-text widgets.

The LLM frequently returns plain text such as ``^3P_2``, ``1s^2 2p^6`` or
``Fe^{16+}``.  Rendering that literally looks unprofessional, so this module
escapes the text, converts the notation to HTML superscript/subscript, and
then lets Python-Markdown handle headings, lists, tables and code fences.
"""

from __future__ import annotations

import html
import re
from typing import Any

_GREEK = {
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\zeta": "ζ", r"\eta": "η", r"\theta": "θ",
    r"\iota": "ι", r"\kappa": "κ", r"\lambda": "λ", r"\mu": "μ",
    r"\nu": "ν", r"\xi": "ξ", r"\pi": "π", r"\rho": "ρ",
    r"\sigma": "σ", r"\tau": "τ", r"\upsilon": "υ", r"\phi": "φ",
    r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Xi": "Ξ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Phi": "Φ",
    r"\Psi": "Ψ", r"\Omega": "Ω",
}
_SYMBOLS = {
    r"\times": "×", r"\pm": "±", r"\mp": "∓", r"\approx": "≈",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
    r"\infty": "∞", r"\rightarrow": "→", r"\to": "→",
    r"\leftarrow": "←", r"\cdot": "·", r"\degree": "°",
}

_CODE_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# 2P_3/2, ^3P_2, ^1S_0^o, 2P_{3/2}
_TERM_RE = re.compile(
    r"\^(\d+)\s*([SPDFGHIK])\s*(?:_?\{?(\d+(?:/\d+)?)\}?)?\s*(\^?\s*[o°])?",
    re.I,
)
_CONFIG_RE = re.compile(r"\b(\d+[spdfg])\s*\^\s*\{?(\d+)\}?", re.I)
_CHARGE_RE = re.compile(
    r"\b([A-Z][a-z]?)\s*\^\s*\{?([0-9]*[+\-−])\}?", re.I
)
_TRAILING_CHARGE_RE = re.compile(r"\b([A-Z][a-z]?)([+\-−])(?![A-Za-z])")
_BRACED_SUP_RE = re.compile(r"\^\{([^{}]+)\}")
_BRACED_SUB_RE = re.compile(r"_\{([^{}]+)\}")
_SUP_RE = re.compile(r"\^([0-9+\-−]+)")
_SUB_RE = re.compile(r"_([0-9]+)")
_COMMAND_RE = re.compile(r"\\(mathrm|text|operatorname|mathit)\{([^{}]*)\}")


def _protect_code(text: str) -> tuple[str, list[str]]:
    blocks: list[str] = []

    def save(match: re.Match[str]) -> str:
        blocks.append(match.group(0))
        return f"\x00CODE{len(blocks) - 1}\x00"

    body = _CODE_FENCE_RE.sub(save, text)
    body = _INLINE_CODE_RE.sub(save, body)
    return body, blocks


def _restore_code(text: str, blocks: list[str]) -> str:
    def restore(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(blocks):
            return ""
        raw = blocks[index]
        if raw.startswith("```"):
            lines = raw.splitlines()
            code = "\n".join(lines[1:-1]) if len(lines) >= 2 else ""
            escaped = html.escape(code)
        else:
            escaped = html.escape(raw.strip("`"))
        return f"<pre class=\"md-code\"><code>{escaped}</code></pre>"

    return re.sub(r"\x00CODE(\d+)\x00", restore, text)


def atomic_symbols_to_html(text: str) -> str:
    """Convert common atomic notation into HTML sup/sub tags.

    Input must already be HTML-escaped; the function only inserts our own
    tags.  It intentionally leaves ordinary words and code blocks untouched
    via the caller's code-protection step.
    """

    body = str(text or "")
    body = _COMMAND_RE.sub(lambda m: m.group(2), body)
    for macro, symbol in {**_GREEK, **_SYMBOLS}.items():
        body = body.replace(macro, symbol)

    # Term symbols must run before the generic ^ / _ conversion.
    def term(match: re.Match[str]) -> str:
        multiplicity, letter, sub, parity = match.groups()
        result = f"<sup>{multiplicity}</sup>{letter.upper()}"
        if sub:
            result += f"<sub>{sub}</sub>"
        if parity:
            result += "°"
        return result

    body = _TERM_RE.sub(term, body)
    body = _CONFIG_RE.sub(lambda m: f"{m.group(1)}<sup>{m.group(2)}</sup>", body)
    body = _CHARGE_RE.sub(lambda m: f"{m.group(1)}<sup>{m.group(2)}</sup>", body)
    body = _TRAILING_CHARGE_RE.sub(
        lambda m: f"{m.group(1)}<sup>{'−' if m.group(2) == '-' else m.group(2)}</sup>",
        body,
    )
    body = _BRACED_SUP_RE.sub(r"<sup>\1</sup>", body)
    body = _BRACED_SUB_RE.sub(r"<sub>\1</sub>", body)
    body = _SUP_RE.sub(r"<sup>\1</sup>", body)
    body = _SUB_RE.sub(r"<sub>\1</sub>", body)
    # Inline dollar math: remove delimiters after the symbol conversion.
    body = re.sub(r"\$([^$]+)\$", r'<span class="md-math">\1</span>', body)
    return body


def _premise_list_html(text: str) -> str | None:
    parts = [part.strip() for part in re.split(r"[；;]\s*", str(text or "")) if part.strip()]
    if len(parts) < 2:
        return None
    labeled = sum(1 for part in parts if "：" in part or ":" in part)
    if labeled < max(1, len(parts) // 2):
        return None
    items = "".join(f"<li>{part}</li>" for part in parts)
    return f'<ul class="md-premises">{items}</ul>'


def render_atomic_markdown(
    text: Any,
    *,
    premises: bool = False,
    inline: bool = False,
) -> str:
    """Render Markdown with atomic symbols.  Returns HTML for QTextBrowser."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    protected, code_blocks = _protect_code(raw)
    escaped = html.escape(protected, quote=False)
    converted = atomic_symbols_to_html(escaped)

    if premises:
        list_html = _premise_list_html(converted)
        if list_html is not None:
            return list_html

    try:
        import markdown

        rendered = markdown.markdown(
            converted,
            extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
            output_format="html5",
        )
    except Exception:
        rendered = converted.replace("\n", "<br>")
    if inline:
        rendered = re.sub(r"^<p>(.*)</p>$", r"\1", rendered, flags=re.S)
    rendered = _restore_code(rendered, code_blocks)
    rendered = re.sub(r"<p>\s*(<pre.*?</pre>)\s*</p>", "\\1", rendered, flags=re.S)
    return rendered


__all__ = ["atomic_symbols_to_html", "render_atomic_markdown"]
