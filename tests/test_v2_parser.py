from pathlib import Path

import pytest

from builtin_models.model_registry import ModelRegistry
from crawler.title_gatekeeper import TitleGatekeeper
from parser.conclusion_tagger import (
    FLAG_BOUNDARY,
    FLAG_EXISTENCE,
    FLAG_MECHANISM,
    FLAG_QUANTITATIVE,
    FLAG_RECOMMENDATION,
    FLAG_TREND,
    tag_text,
)
from parser.consensus_conflict_engine import ConsensusConflictEngine
from parser.context_bundler import ContextBundler
from parser.four_channel_radar import FourChannelRadar
from parser.book_locator import BookLocator
from parser.pdf_to_markdown import PDFParser, latex_source_to_markdown

SAMPLE = """# Relativistic CI calculations for Fe XVII

We calculate Fe XVII with GRASP2018 using a Dirac-Coulomb-Breit Hamiltonian. The ion is treated in the Ne-like closed-shell approximation.

## II.B Active space and convergence

For Fe XVII, the reference configuration is [Ne] 2s2 2p6.

Model B is defined as opening the 2s and 2p shells and expanding the active space up to n=5, l<=2.

Table II: Active-space settings for Model B and Model C.
| Model | Active space | CSF | Convergence |
| --- | --- | --- | --- |
| Model B | n=5, l<=2 | 12000 | 1e-8 |
| Model C | n=6, l<=3 | 48000 | 5e-9 |

As shown in Table II, the length and velocity gauge results agree within 1% and converge, demonstrating that Model B is sufficient for the present accuracy.

## II.C Hamiltonian

The Hamiltonian is

$$
H = H_D + B_{ij}
$$

where B_ij is the Breit interaction and QED corrections are added perturbatively.

## II.D Workflow

The GRASP workflow is:

```bash
rmcdhf < input
rci < input
```
"""


def _document():
    return PDFParser().parse_markdown(
        SAMPLE, title="Test paper", doc_id="DOC-PAP-2024-ABCDEF"
    )


def test_markdown_sections_table_and_formula():
    doc = _document()
    paths = [section.path for section in doc.sections]
    assert any("II.B Active space" in path for path in paths)
    section = next(section for section in doc.sections if "II.B Active space" in section.path)
    assert any("Model B is defined" in paragraph for paragraph in section.paragraphs)
    assert section.tables and "Table II" in section.tables[0].caption
    formula_section = next(section for section in doc.sections if "II.C" in section.path)
    assert formula_section.formulas
    code_section = next(section for section in doc.sections if "II.D" in section.path)
    assert code_section.code_blocks


def test_four_channel_radar():
    doc = _document()
    radar = FourChannelRadar().scan(doc)
    assert radar.conclusions
    assert radar.strategy_tables
    assert radar.formulas
    assert radar.code_blocks
    assert any(hit.channel == "CONCLUSION" for hit in radar.conclusions)
    assert radar.strategy_tables[0].rows == 2
    assert "Table II" in radar.strategy_tables[0].caption


def test_context_bundle_stitches_premise_definitions_and_focus():
    doc = _document()
    radar = FourChannelRadar().scan(doc)
    bundles = ContextBundler().build(doc, radar, doc_id="DOC-PAP-2024-ABCDEF")
    bundle = next(
        item
        for item in bundles
        if "within 1%" in item.focal_sentence
        or "length and velocity" in item.focal_sentence
    )
    assert "Fe XVII" in bundle.global_premise_paragraph
    assert any("Model B is defined" in item for item in bundle.backtraced_definitions)
    assert any(
        "Table II" in item and "| Model B |" in item
        for item in bundle.backtraced_definitions
    )
    assert "⟪FOCUS⟫" in bundle.focal_window_markdown
    assert bundle.preliminary_tags_bitmask & FLAG_QUANTITATIVE
    assert bundle.preliminary_tags_bitmask & FLAG_TREND
    assert "CONCLUSION" in bundle.detected_channels or "STRATEGY_TABLE" in bundle.detected_channels
    assert "DOC-PAP-2024-ABCDEF" in bundle.prompt_markdown()


def test_book_locator_scores_strategy_sections():
    doc = _document()
    windows = BookLocator().locate(
        doc, keywords=["active space", "convergence", "Breit"], max_windows=5
    )
    assert windows
    top = windows[0]
    assert "Active space" in top.section_path or "Hamiltonian" in top.section_path
    assert top.density > 0
    assert top.matched_keywords
    assert "active space" in " ".join(top.matched_keywords).lower()


def test_conclusion_tagger_six_flags():
    result = tag_text(
        "The transition rate increases by 12% because the Breit interaction is included; "
        "however, the method fails to converge for n=8, so we recommend n=6."
    )
    assert result.bitmask & FLAG_QUANTITATIVE
    assert result.bitmask & FLAG_TREND
    assert result.bitmask & FLAG_MECHANISM
    assert result.bitmask & FLAG_BOUNDARY
    assert result.bitmask & FLAG_RECOMMENDATION


def test_title_gatekeeper_accepts_and_rejects():
    gate = TitleGatekeeper()
    accepted = gate.evaluate(
        "Relativistic MCDHF and RCI calculations of E1 transitions in Fe XVII: "
        "core-valence correlation and Breit interaction",
        journal="Physical Review A",
    )
    assert accepted.accepted and accepted.score >= 40
    assert accepted.authority_weight == 1.5
    rejected = gate.evaluate(
        "Tokamak transport simulation of a laser-produced plasma plume",
        abstract="A purely experimental plasma study in a tokamak device.",
    )
    assert rejected.rejected and not rejected.accepted


def test_consensus_and_conflict_engine():
    engine = ConsensusConflictEngine()
    a = {
        "knw_id": "KNW-STRAT-2024-AAAAAA",
        "summary_zh": "The Breit interaction increases the transition rate and improves agreement with experiment.",
        "supporting_doc_ids": ["DOC-PAP-2024-AAAAAA"],
        "consensus_count": 1,
    }
    b = {
        "knw_id": "KNW-STRAT-2024-BBBBBB",
        "summary_zh": "The Breit interaction decreases the transition rate and worsens agreement with experiment.",
        "supporting_doc_ids": ["DOC-PAP-2024-BBBBBB"],
        "consensus_count": 1,
    }
    conflict = engine.detect_conflict(a["summary_zh"], b["summary_zh"])
    assert conflict.conflict
    result = engine.consolidate(a, [a])
    assert result.duplicate
    assert result.target_id == a["knw_id"]
    assert result.consensus_count >= 2
    result_conflict = engine.consolidate(a, [b])
    assert not result_conflict.duplicate
    assert result_conflict.has_conflict
    assert b["knw_id"] in result_conflict.conflict_ids


def test_model_registry_status_and_spacy(qapp, tmp_path: Path):
    registry = ModelRegistry(tmp_path / "workspace")
    status = {item["name"]: item for item in registry.status()}
    assert "spacy_en" in status
    assert status["spacy_en"]["present"] is True
    assert status["mini_cluster_embed"]["present"] is False
    assert registry.spacy_pipeline() is not None


def test_latex_source_to_markdown(tmp_path: Path):
    import tarfile

    archive = tmp_path / "source.tar.gz"
    tex = r"""\documentclass{article}
\begin{document}
\section{Methods}
We calculate Fe XVII with GRASP2018.
\begin{equation}
H = H_D + B_{ij}
\end{equation}
\subsection{Active space}
The active space is expanded to n=7.
\end{document}
"""
    source = tmp_path / "main.tex"
    source.write_text(tex, encoding="utf-8")
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(source, arcname="main.tex")
    markdown = latex_source_to_markdown(archive)
    assert "## Methods" in markdown
    assert "### Active space" in markdown
    assert "H = H_D + B_ij" in markdown
    assert "GRASP2018" in markdown
    assert "\\begin{equation}" not in markdown
    parsed = PDFParser().parse_markdown(markdown, title="LaTeX paper")
    assert any("Methods" in section.path for section in parsed.sections)


def test_pdf_parser_local(tmp_path: Path):
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "sample.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "II. Methods", fontsize=16)
    page.insert_text((72, 110), "We calculate Fe XVII with GRASP2018.", fontsize=11)
    page.insert_text((72, 132), "Model B is defined as opening the 2s shell.", fontsize=11)
    document.save(pdf)
    document.close()

    parsed = PDFParser().parse(pdf, doc_id="DOC-PAP-2024-ABCDEF")
    assert parsed.page_count == 1
    assert any("Fe XVII" in paragraph for section in parsed.sections for paragraph in section.paragraphs)
    assert any("GRASP2018" in section.text() for section in parsed.sections)
