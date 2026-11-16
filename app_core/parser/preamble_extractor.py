"""Preamble Extractor: Accurate Ingestion of Global Upfront Declarations.

In atomic physics and computational physics literature, authors frequently state
global premises once at the beginning (Section 1 / Section 2 or Page 1 footnotes):
- Atomic units convention: "Throughout this paper, atomic units are used unless stated otherwise."
- Nuclear model: "The Fermi two-parameter distribution is adopted for nuclear charge with t = 2.3 fm."
- Frozen core: "The [Ne] core is kept frozen in all multiconfiguration calculations."
- Gauge convention: "All transition probabilities are computed in the Coulomb (velocity) gauge unless noted."

This module scans early document sections to harvest these global conditions,
storing them in a document-level register so they can be injected into every
ContextBundle, completely preventing cross-section context detachment.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

_PREAMBLE_CUES = re.compile(
    r"\b(throughout this (?:work|paper|article|study|thesis)|"
    r"in all (?:our )?calculations|"
    r"unless (?:otherwise )?(?:stated|specified|noted)|"
    r"we assume throughout|"
    r"hereafter|"
    r"adopted throughout|"
    r"is used throughout|"
    r"are used throughout|"
    r"are adopted throughout|"
    r"in the following(?:,\s*we)?|"
    r"for all calculations)\b",
    re.I,
)

_PREAMBLE_DOMAINS = re.compile(
    r"(atomic units|a\.u\.|fermi|nuclear charge|charge distribution|"
    r"frozen core|gauge|speed of light|c =|convergence threshold|"
    r"potential|hamiltonian|configuration|active space|rydberg|hartree|"
    r"basis set|relativistic correction|breit|qed)",
    re.I,
)

_SECTION_TITLE_SKIP = re.compile(
    r"(acknowledg|reference|author|appendix|table|figure)",
    re.I,
)


def extract_document_preamble(document: Any, max_preambles: int = 6) -> list[str]:
    """Extract global upfront condition declarations from a ParsedDocument."""
    if not document or not hasattr(document, "sections"):
        return []

    found: list[str] = []
    seen: set[str] = set()

    # Scan early sections: Abstract, Introduction, Theory, Computational Method
    early_sections = document.sections[:4] if len(document.sections) > 4 else document.sections

    for section in early_sections:
        title = getattr(section, "title", "") or ""
        if _SECTION_TITLE_SKIP.search(title):
            continue

        paragraphs = getattr(section, "paragraphs", []) or []
        for paragraph in paragraphs:
            # Skip code blocks and markdown tables
            p_strip = str(paragraph).strip()
            if not p_strip or p_strip.startswith(("```", "|", "$$")):
                continue

            # Split paragraph into candidate sentences
            sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", p_strip)
            for sent in sentences:
                sent_clean = sent.strip()
                if len(sent_clean) < 20 or len(sent_clean) > 400:
                    continue

                # Must match a preamble cue and preferably a domain concept
                is_cue = bool(_PREAMBLE_CUES.search(sent_clean))
                is_domain = bool(_PREAMBLE_DOMAINS.search(sent_clean))

                if is_cue and is_domain:
                    # Normalize whitespace
                    normalized = re.sub(r"\s+", " ", sent_clean)
                    norm_key = normalized.lower()
                    if norm_key not in seen:
                        seen.add(norm_key)
                        found.append(normalized)
                        if len(found) >= max_preambles:
                            return found

    return found
