"""Six-dimensional conclusion tagging (bitmask).

Bit layout (UpdateTask_01 §1.2 / §2.2):

===================  ======  ==========================================
bit                    value   meaning
===================  ======  ==========================================
QUANTITATIVE              1  量化：数字、单位、误差、比值
TREND                     2  趋势：增大/减小/收敛/优于
MECHANISM                 4  机理：因为/源于/机制
EXISTENCE                 8  存在：观察到/发现/表明
BOUNDARY_LIMITATION      16  边界局限：仅当/失效/崩塌/不适用
RECOMMENDATION           32  建议决策：推荐/应采用/优先
===================  ======  ==========================================

The tagger is intentionally CPU-only and deterministic.  It does not need
spaCy; optional dependency parsing is used elsewhere in the radar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FLAG_QUANTITATIVE = 1
FLAG_TREND = 2
FLAG_MECHANISM = 4
FLAG_EXISTENCE = 8
FLAG_BOUNDARY = 16
FLAG_RECOMMENDATION = 32

FLAG_LABELS = {
    FLAG_QUANTITATIVE: "量化",
    FLAG_TREND: "趋势",
    FLAG_MECHANISM: "机理",
    FLAG_EXISTENCE: "存在",
    FLAG_BOUNDARY: "局限",
    FLAG_RECOMMENDATION: "建议策略",
}

_PATTERNS: dict[int, list[re.Pattern[str]]] = {
    FLAG_QUANTITATIVE: [
        re.compile(
            r"\b\d+(?:\.\d+)?\s*(?:%|percent|a\.u\.|eV|keV|MeV|cm-?1|cm\^?-1|"
            r"nm|Å|angstrom|MHz|GHz|K|Ry|hartree|bohr|au|Debye|D)"
            r"(?![A-Za-z0-9])",
            re.I,
        ),
        re.compile(r"误差|不确定度|偏差|accuracy|uncertainty|within\s+\d", re.I),
        re.compile(r"\b\d+(?:\.\d+)?\s*(?:times|orders?|fold)\b", re.I),
    ],
    FLAG_TREND: [
        re.compile(
            r"\b(increase[sd]?|decrease[sd]?|improve[sd]?|reduce[sd]?|"
            r"enhance[sd]?|lower(?:s|ed)?|higher|converge[sd]?|diverge[sd]?|"
            r"overestimate[sd]?|underestimate[sd]?|outperform[sd]?|"
            r"shift(?:s|ed)?|deviate[sd]?)\b",
            re.I,
        ),
        re.compile(r"增大|减小|提高|降低|增强|减弱|收敛|发散|偏高|偏低|优于|劣于"),
    ],
    FLAG_MECHANISM: [
        re.compile(
            r"\b(because|due to|owing to|arises? from|originates? from|"
            r"attribut(?:e|ed|able) to|mechanism|driven by|as a result of|"
            r"caused by|results? from)\b",
            re.I,
        ),
        re.compile(r"由于|源于|源于|归因于|机制|机理|原因是|导致"),
    ],
    FLAG_EXISTENCE: [
        re.compile(
            r"\b(observe[sd]?|found|find|finds|demonstrate[sd]?|show(?:s|n|ed)?|"
            r"reveal(?:s|ed)?|identify|identified|present(?:s|ed)?|"
            r"there (?:is|are)|exists?)\b",
            re.I,
        ),
        re.compile(r"观察到|发现|表明|显示|存在|揭示|证实"),
    ],
    FLAG_BOUNDARY: [
        re.compile(
            r"\b(only if|only when|provided that|limited to|valid (?:for|when)|"
            r"restricted to|up to|unless|cannot|fail(?:s|ed)? to converge|"
            r"inapplicable|breakdown|variational collapse|intruder state|"
            r"discrepancy|disagreement)\b",
            re.I,
        ),
        re.compile(r"仅当|仅在|限于|适用于|不适用|失效|崩塌|闯入态|偏差较大|不一致|无法收敛"),
    ],
    FLAG_RECOMMENDATION: [
        re.compile(
            r"\b(recommend(?:s|ed|ation)?|suggest(?:s|ed|ion)?|should|"
            r"prefer(?:s|red|able)?|we (?:use|adopt|choose)|is preferred|"
            r"best practice|strategy|workflow)\b",
            re.I,
        ),
        re.compile(r"建议|推荐|应采用|宜采用|优先|最佳实践|策略"),
    ],
}

_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s*|\n+")


@dataclass
class TagResult:
    bitmask: int = 0
    labels: list[str] = field(default_factory=list)
    matches: dict[str, list[str]] = field(default_factory=dict)

    def has(self, flag: int) -> bool:
        return bool(self.bitmask & flag)

    def as_dict(self) -> dict[str, object]:
        return {
            "bitmask": self.bitmask,
            "labels": self.labels,
            "matches": self.matches,
        }


def tag_text(text: str | None) -> TagResult:
    """Return the multi-label bitmask for a sentence or paragraph."""
    body = str(text or "").strip()
    result = TagResult()
    if not body:
        return result
    for flag, patterns in _PATTERNS.items():
        found: list[str] = []
        for pattern in patterns:
            for match in pattern.finditer(body):
                value = match.group(0).strip()
                if value and value not in found:
                    found.append(value)
        if found:
            result.bitmask |= flag
            result.labels.append(FLAG_LABELS[flag])
            result.matches[FLAG_LABELS[flag]] = found[:6]
    return result


def tag_sentences(text: str | None) -> list[dict[str, object]]:
    sentences = [part.strip() for part in _SENTENCE_RE.split(str(text or "")) if part.strip()]
    return [{"sentence": sentence, **tag_text(sentence).as_dict()} for sentence in sentences]


def labels_for_mask(mask: int) -> list[str]:
    return [label for flag, label in FLAG_LABELS.items() if mask & flag]


def mask_has(mask: int, flag: int) -> bool:
    return bool(int(mask or 0) & int(flag))
