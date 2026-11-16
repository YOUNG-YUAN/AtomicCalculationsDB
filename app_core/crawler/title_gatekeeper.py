"""Three-second title gatekeeper (UpdateTask_01 §2.1).

Before spending time on a download, score the candidate metadata:

* a pure experimental / clinical / astrophysical observation is vetoed when no
  atomic-structure theory term is present;
* methods +40, target physical processes +30, strategy keywords +30;
* a score >= 40 enters the download queue.

The gatekeeper also assigns the scientific authority weight used later by the
three-step retriever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

METHOD_TERMS = (
    "mcdhf",
    "rci",
    "rmbpt",
    "coupled-cluster",
    "coupled cluster",
    "ccsd",
    "ccsd(t)",
    "ci+mbpt",
    "configuration interaction",
    "dirac-fock",
    "dirac hartree-fock",
    "dirac-hartree-fock",
    "ab initio",
    "b-spline",
    "bspline",
    "grasp",
    "fac",
    "ambit",
    "dirac",
    "relativistic many-body",
    "multiconfiguration",
)
PROCESS_TERMS = (
    "transition",
    "transitions",
    "hyperfine",
    "hfs",
    "isotope shift",
    "polarizability",
    "energy level",
    "energy levels",
    "oscillator strength",
    "lifetime",
    "forbidden line",
    "photoionization",
    "recombination",
    "ionization potential",
    "fine structure",
)
STRATEGY_TERMS = (
    "correlation",
    "core-valence",
    "core valence",
    "active space",
    "convergence",
    "breit",
    "qed",
    "configuration interaction",
    "basis set",
    "truncation",
    "extrapolation",
    "variational",
    "intruder",
    "gauge",
    "electron correlation",
)
EXPERIMENTAL_TERMS = (
    "tokamak transport simulation",
    "solar flare observation",
    "chandra x-ray spectrum",
    "laser-produced plasma plume",
    "clinical trial",
    "patient cohort",
    "in vivo",
    "in vitro",
    "spectrometer measurement",
    "beam-foil measurement",
    "experimental spectrum",
    "observation of",
)
THEORY_TERMS = METHOD_TERMS + (
    "atomic structure",
    "atomic data",
    "theoretical",
    "calculation",
    "computational",
    "ab initio",
)

_AUTHORITY_VENUES = re.compile(
    r"(physical review a|atomic data and nuclear data tables|"
    r"computer physics communications|journal of physics b|"
    r"physics reports|reviews of modern physics|journal of physical and chemical reference data|"
    r"springer series|oxford|review)",
    re.I,
)


@dataclass
class GatekeeperResult:
    accepted: bool
    rejected: bool
    score: int
    reasons: list[str] = field(default_factory=list)
    method_hits: list[str] = field(default_factory=list)
    process_hits: list[str] = field(default_factory=list)
    strategy_hits: list[str] = field(default_factory=list)
    authority_weight: float = 1.0

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "score": self.score,
            "reasons": self.reasons,
            "method_hits": self.method_hits,
            "process_hits": self.process_hits,
            "strategy_hits": self.strategy_hits,
            "authority_weight": self.authority_weight,
        }


def _find(text: str, terms: Iterable[str]) -> list[str]:
    lowered = text.lower()
    hits: list[str] = []
    for term in terms:
        if term.lower() in lowered and term not in hits:
            hits.append(term)
    return hits


class TitleGatekeeper:
    def __init__(self, *, threshold: int = 40):
        self.threshold = threshold

    def evaluate(
        self,
        title: str,
        *,
        abstract: str = "",
        keywords: Iterable[str] | str | None = None,
        journal: str = "",
        doc_type: str = "PAP",
    ) -> GatekeeperResult:
        if isinstance(keywords, str):
            keyword_text = keywords
        else:
            keyword_text = " ".join(str(item) for item in (keywords or []))
        text = " ".join((title or "", abstract or "", keyword_text)).strip()
        lowered = text.lower()

        method_hits = _find(text, METHOD_TERMS)
        process_hits = _find(text, PROCESS_TERMS)
        strategy_hits = _find(text, STRATEGY_TERMS)

        experimental_hits = _find(text, EXPERIMENTAL_TERMS)
        theory_present = bool(_find(text, THEORY_TERMS)) or bool(method_hits)
        rejected = bool(experimental_hits) and not theory_present

        reasons: list[str] = []
        if rejected:
            reasons.append(
                "一票否决：命中实验/临床/纯观测词且未见原子结构理论词 "
                + ", ".join(experimental_hits[:3])
            )
            score = 0
        else:
            score = 0
            if method_hits:
                score += 40
                reasons.append("方法 +40: " + ", ".join(method_hits[:3]))
            if process_hits:
                score += 30
                reasons.append("物理过程 +30: " + ", ".join(process_hits[:3]))
            if strategy_hits:
                score += 30
                reasons.append("策略关键词 +30: " + ", ".join(strategy_hits[:3]))
            score = min(100, score)

        authority = self._authority(journal, doc_type)
        accepted = (not rejected) and score >= self.threshold
        if not accepted and not rejected:
            reasons.append(f"得分 {score} < 阈值 {self.threshold}，不进入下载队列")
        return GatekeeperResult(
            accepted=accepted,
            rejected=rejected,
            score=score,
            reasons=reasons,
            method_hits=method_hits,
            process_hits=process_hits,
            strategy_hits=strategy_hits,
            authority_weight=authority,
        )

    @staticmethod
    def _authority(journal: str, doc_type: str) -> float:
        kind = (doc_type or "").upper()
        if kind in ("BOK", "REV"):
            return 1.5
        if kind == "MAN":
            return 1.2
        if journal and _AUTHORITY_VENUES.search(journal):
            return 1.5
        return 1.0
