"""Three-step hybrid retrieval (UpdateTask_01 §4).

Step 1  Query intent parsing + metadata pre-filtering
Step 2  FTS5 BM25 + Qdrant ANN recall, fused with RRF (k=60)
Step 3  Reranker + consensus / authority / recency fusion → Top 10

Works without any model: FTS-only retrieval and a deterministic lexical
rerank fallback are always available.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

from db.id_generator import classify_id
from db.qdrant_manager import QdrantManager, QdrantUnavailable
from db.sqlite_manager import SQLiteManager
from parser.conclusion_tagger import (
    FLAG_BOUNDARY,
    FLAG_EXISTENCE,
    FLAG_MECHANISM,
    FLAG_QUANTITATIVE,
    FLAG_RECOMMENDATION,
    FLAG_TREND,
)

log = logging.getLogger(__name__)


class RetrievalError(RuntimeError):
    pass


_PERIODIC = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni "
    "Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe "
    "Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg "
    "Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg "
    "Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og"
).split()
_Z_BY_SYMBOL = {symbol: index + 1 for index, symbol in enumerate(_PERIODIC)}

_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

_ISOELECTRONIC_ZH = {
    "类氢": "H-like",
    "类氦": "He-like",
    "类锂": "Li-like",
    "类铍": "Be-like",
    "类硼": "B-like",
    "类碳": "C-like",
    "类氮": "N-like",
    "类氧": "O-like",
    "类氟": "F-like",
    "类氖": "Ne-like",
    "类钠": "Na-like",
    "类镁": "Mg-like",
    "类铝": "Al-like",
    "类硅": "Si-like",
    "类氩": "Ar-like",
    "类铁": "Fe-like",
    "类镍": "Ni-like",
    "类铜": "Cu-like",
}
_ELEMENT_NAMES = {
    "hydrogen": "H", "helium": "He", "lithium": "Li", "beryllium": "Be",
    "boron": "B", "carbon": "C", "nitrogen": "N", "oxygen": "O",
    "fluorine": "F", "neon": "Ne", "sodium": "Na", "magnesium": "Mg",
    "aluminum": "Al", "aluminium": "Al", "silicon": "Si", "phosphorus": "P",
    "sulfur": "S", "chlorine": "Cl", "argon": "Ar", "iron": "Fe",
    "nickel": "Ni", "copper": "Cu", "zinc": "Zn", "tungsten": "W",
    "uranium": "U", "xenon": "Xe", "krypton": "Kr",
}

_METHOD_PATTERNS = (
    ("CI+MBPT", re.compile(r"ci\s*\+\s*mbpt", re.I)),
    ("MCDHF", re.compile(r"mcdhf|multiconfiguration\s+dirac", re.I)),
    ("RMBPT", re.compile(r"rmbpt|relativistic\s+many[- ]body", re.I)),
    ("RCC", re.compile(r"\brcc\b|relativistic\s+coupled[- ]cluster", re.I)),
    ("CC", re.compile(r"coupled[- ]cluster|\bccsd(?:\(t\))?\b|\bcc\b", re.I)),
    ("RCI", re.compile(r"\brci\b|relativistic\s+configuration\s+interaction", re.I)),
    ("CI", re.compile(r"\bci\b|configuration\s+interaction", re.I)),
    ("MBPT", re.compile(r"\bmbpt\b", re.I)),
    ("GRASP", re.compile(r"\bgrasp\d*\b", re.I)),
    ("FAC", re.compile(r"\bfac\b", re.I)),
    ("AMBiT", re.compile(r"\bambit\b", re.I)),
    ("DIRAC", re.compile(r"\bdirac\b", re.I)),
    ("B-spline", re.compile(r"b[- ]spline", re.I)),
    ("Dirac-Fock", re.compile(r"dirac[- ]fock", re.I)),
    ("ab initio", re.compile(r"ab\s+initio", re.I)),
)

_PROCESS_PATTERNS = (
    ("E1_transition", re.compile(r"\be1\b(?:\s+transitions?)?|电偶极|electric\s+dipole", re.I)),
    ("E2_transition", re.compile(r"\be2\b(?:\s+transitions?)?|电四极|electric\s+quadrupole", re.I)),
    ("M1_transition", re.compile(r"\bm1\b(?:\s+transitions?)?|磁偶极|magnetic\s+dipole", re.I)),
    ("HFS", re.compile(r"\bhfs\b|hyperfine|超精细", re.I)),
    ("isotope_shift", re.compile(r"isotope\s+shift|同位素位移", re.I)),
    ("polarizability", re.compile(r"polarizabilit|极化率", re.I)),
    ("energy_levels", re.compile(r"energy\s+levels?|能级", re.I)),
    ("oscillator_strength", re.compile(r"oscillator\s+strength|振子强度", re.I)),
    ("lifetime", re.compile(r"\blifetimes?\b|寿命", re.I)),
    ("photoionization", re.compile(r"photoionization|光电离", re.I)),
    ("recombination", re.compile(r"recombination|复合", re.I)),
    ("forbidden", re.compile(r"forbidden|禁戒", re.I)),
    ("transition", re.compile(r"\btransitions?\b|跃迁", re.I)),
)

_CARD_TYPE_PATTERNS = (
    ("STRAT", re.compile(r"策略|计算方案|strategy", re.I)),
    ("MODEL", re.compile(r"模型|model", re.I)),
    ("CODE", re.compile(r"代码|程序|工作流|code|workflow", re.I)),
    ("PITFALL", re.compile(r"排错|踩坑|pitfall|troubleshoot", re.I)),
    ("CONC", re.compile(r"结论|conclusion", re.I)),
)

_FLAG_PATTERNS = (
    (FLAG_QUANTITATIVE, re.compile(r"量化|数值|quantitative|误差|uncertainty", re.I)),
    (FLAG_TREND, re.compile(r"趋势|trend|增大|减小|increase|decrease", re.I)),
    (FLAG_MECHANISM, re.compile(r"机理|机制|mechanism|because|由于", re.I)),
    (FLAG_EXISTENCE, re.compile(r"存在|existence|观察到|found|observe", re.I)),
    (FLAG_BOUNDARY, re.compile(r"局限|边界|limitation|boundary|适用条件", re.I)),
    (FLAG_RECOMMENDATION, re.compile(r"建议|推荐|recommendation|strategy|决策", re.I)),
)


def _roman_to_int(value: str) -> int | None:
    text = (value or "").upper()
    total = 0
    previous = 0
    for char in reversed(text):
        number = _ROMAN_VALUES.get(char)
        if number is None:
            return None
        if number < previous:
            total -= number
        else:
            total += number
            previous = number
    return total or None


@dataclass
class QueryIntent:
    raw: str
    residual: str
    filters: dict[str, Any] = field(default_factory=dict)
    exact_id: dict[str, str] | None = None
    matched_terms: list[str] = field(default_factory=list)
    conclusion_flags: int = 0
    is_master_query: bool = False

    def chip_labels(self) -> list[str]:
        labels: list[str] = []
        for key, value in self.filters.items():
            if value in (None, "", 0):
                continue
            if key == "conclusion_flags":
                continue
            labels.append(f"{key}={value}")
        if self.conclusion_flags:
            labels.append(f"结论类型={self.conclusion_flags}")
        return labels

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "residual": self.residual,
            "filters": dict(self.filters),
            "exact_id": self.exact_id,
            "matched_terms": list(self.matched_terms),
            "conclusion_flags": self.conclusion_flags,
            "is_master_query": self.is_master_query,
        }


def _remove_terms(text: str, terms: Iterable[str]) -> str:
    result = text
    for term in sorted(set(terms), key=len, reverse=True):
        if not term:
            continue
        result = re.sub(re.escape(term), " ", result, flags=re.I)
    result = re.sub(r"[，,;；:：=+/|]+", " ", result)
    return " ".join(result.split()).strip()


def parse_intent(query: str) -> QueryIntent:
    raw = " ".join(str(query or "").split())
    intent = QueryIntent(raw=raw, residual=raw)
    if not raw:
        return intent

    exact = classify_id(raw)
    if exact:
        intent.exact_id = exact
        intent.residual = ""
        return intent

    working = raw
    removed: list[str] = []

    # -------------------------------------------------------- isoelectronic
    for zh, canonical in _ISOELECTRONIC_ZH.items():
        if zh in working:
            intent.filters["isoelectronic"] = canonical
            intent.matched_terms.append(zh)
            working = working.replace(zh, " ")
            removed.append(zh)
    for match in re.finditer(
        r"\b([A-Za-z]+)-like\b", working, re.I
    ):
        word = match.group(1)
        canonical_symbol = _ELEMENT_NAMES.get(word.lower(), word.capitalize())
        if canonical_symbol in _Z_BY_SYMBOL:
            canonical = f"{canonical_symbol}-like"
            intent.filters["isoelectronic"] = canonical
            intent.matched_terms.append(match.group(0))
            removed.append(match.group(0))
    if removed:
        working = _remove_terms(working, removed)
        removed.clear()

    # ---------------------------------------------------- element + ion stage
    ion_match = re.search(r"\b([A-Z][a-z]?)\s+([IVXLC]+)\b", working)
    if ion_match and ion_match.group(1) in _Z_BY_SYMBOL:
        element = ion_match.group(1)
        roman = ion_match.group(2).upper()
        if _roman_to_int(roman):
            intent.filters["element"] = element
            intent.filters["z_num"] = _Z_BY_SYMBOL[element]
            intent.filters["ion_stage"] = f"{element} {roman}"
            intent.matched_terms.append(ion_match.group(0))
            working = _remove_terms(working, [ion_match.group(0)])
    z_match = re.search(r"\bZ\s*=\s*(\d{1,3})\b|原子序数\s*(\d{1,3})", working, re.I)
    if z_match:
        z_num = int(z_match.group(1) or z_match.group(2))
        intent.filters["z_num"] = z_num
        if not intent.filters.get("element") and 1 <= z_num <= 118:
            intent.filters["element"] = _PERIODIC[z_num - 1]
        intent.matched_terms.append(z_match.group(0))
        working = _remove_terms(working, [z_match.group(0)])
    if "element" not in intent.filters:
        # Conservative bare-element detection for common heavy/atomic-physics ions.
        bare = re.search(
            r"\b(Fe|W|U|Xe|Ar|Ne|Kr|Ni|Cu|Zn|Mo|Au|Pb|Bi|Th|Rn|Cs|Ba|Ca|Mg|Al|Si|Cl)\b",
            working,
        )
        if bare:
            intent.filters["element"] = bare.group(1)
            intent.filters["z_num"] = _Z_BY_SYMBOL[bare.group(1)]
            intent.matched_terms.append(bare.group(1))
            working = _remove_terms(working, [bare.group(1)])

    # ---------------------------------------------------------------- method
    methods: list[tuple[str, str]] = []
    for canonical, pattern in _METHOD_PATTERNS:
        match = pattern.search(working)
        if match:
            methods.append((canonical, match.group(0)))
    unique_methods = list(dict.fromkeys(item[0] for item in methods))
    if len(unique_methods) == 1:
        intent.filters["method"] = unique_methods[0]
        intent.matched_terms.append(unique_methods[0])
        working = _remove_terms(working, [item[1] for item in methods])
    elif len(unique_methods) > 1:
        intent.matched_terms.extend(unique_methods)

    # --------------------------------------------------------------- process
    processes: list[tuple[str, str]] = []
    for canonical, pattern in _PROCESS_PATTERNS:
        match = pattern.search(working)
        if match:
            processes.append((canonical, match.group(0)))
    specific_processes = {
        "E1_transition",
        "E2_transition",
        "M1_transition",
        "forbidden",
    }
    if any(item[0] in specific_processes for item in processes):
        processes = [item for item in processes if item[0] != "transition"]
    unique_processes = list(dict.fromkeys(item[0] for item in processes))
    if len(unique_processes) == 1:
        intent.filters["physical_process"] = unique_processes[0]
        intent.matched_terms.append(unique_processes[0])
        working = _remove_terms(working, [item[1] for item in processes])
    elif len(unique_processes) > 1:
        intent.matched_terms.extend(unique_processes)

    # ------------------------------------------------------------- card type
    card_types: list[tuple[str, str]] = []
    for canonical, pattern in _CARD_TYPE_PATTERNS:
        match = pattern.search(working)
        if match:
            card_types.append((canonical, match.group(0)))
    unique_card_types = list(dict.fromkeys(item[0] for item in card_types))
    if len(unique_card_types) == 1:
        intent.filters["card_type"] = unique_card_types[0]
        intent.matched_terms.append(unique_card_types[0])
        working = _remove_terms(working, [item[1] for item in card_types])

    # ------------------------------------------------------ conclusion flags
    flags = 0
    flag_terms: list[str] = []
    for flag, pattern in _FLAG_PATTERNS:
        match = pattern.search(working)
        if match:
            flags |= flag
            intent.matched_terms.append(match.group(0))
            flag_terms.append(match.group(0))
    if flags:
        intent.conclusion_flags = flags
        intent.filters["conclusion_flags"] = flags
        working = _remove_terms(working, flag_terms)

    # --------------------------------------------------------------- L1 hint
    if re.search(r"总纲|master|overview|顶层", raw, re.I):
        intent.is_master_query = True
        intent.filters["card_level"] = "L1"
        intent.matched_terms.append("总纲")
        working = re.sub(r"总纲|master|overview|顶层", " ", working, flags=re.I)

    intent.residual = " ".join(working.split()).strip()
    if not intent.residual:
        intent.residual = ""
    return intent


@dataclass
class RetrievalHit:
    kind: str  # card | master | document | keyword | evidence
    id: str
    title: str
    summary: str = ""
    score: float = 0.0
    card: dict[str, Any] | None = None
    document: dict[str, Any] | None = None
    keyword: dict[str, Any] | None = None
    snippet: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "score": self.score,
            "snippet": self.snippet,
            "card": self.card,
            "document": self.document,
            "keyword": self.keyword,
            "evidence": self.evidence,
            "scores": self.scores,
        }


class HybridRetriever:
    MAX_PAGE_RESULTS = 5_000
    MAX_VECTOR_RESULTS = 1_000
    RERANK_LIMIT = 200
    PAGE_CACHE_SIZE = 3

    def __init__(
        self,
        db: SQLiteManager,
        *,
        qdrant: QdrantManager | None = None,
        embedder: Any = None,
        reranker: Any = None,
        rrf_k: int = 60,
    ):
        self.db = db
        self.qdrant = qdrant
        self.embedder = embedder
        self.reranker = reranker
        self.rrf_k = rrf_k
        self._page_cache: OrderedDict[
            tuple[str, str, str], tuple[list[RetrievalHit], int]
        ] = OrderedDict()

    # ------------------------------------------------------------------ health
    def health(self) -> dict[str, Any]:
        embedding_available = bool(
            getattr(self.embedder, "available", self.embedder is not None)
        ) if self.embedder is not None else False
        return {
            "qdrant": self.qdrant.status().as_dict() if self.qdrant else None,
            "embedding": embedding_available,
            "reranker": self.reranker is not None,
        }

    # ----------------------------------------------------------------- search
    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        extra_filters: Mapping[str, Any] | None = None,
        include_evidence: bool = False,
    ) -> list[RetrievalHit]:
        intent = parse_intent(query)
        if intent.exact_id:
            return self._exact_hit(intent)

        filters: dict[str, Any] = dict(intent.filters)
        if extra_filters:
            for key, value in extra_filters.items():
                if value is None or value == "":
                    continue
                if value == 0 and key not in (
                    "audit_hidden",
                    "audit_delete_suggested",
                    "audit_divergence",
                ):
                    continue
                filters[key] = value
        filters = self._with_default_audit_filter(filters, extra_filters)

        search_text = intent.residual or intent.raw
        fts_results, vector_results = self._collect_candidates(
            search_text,
            filters,
            fts_limit=50,
            vector_limit=50,
        )
        hits = self._rank_candidates(
            query,
            search_text,
            fts_results,
            vector_results,
            max_candidates=50,
            rerank_limit=50,
            include_evidence=include_evidence,
        )
        return hits[: max(1, int(limit))]

    def search_page(
        self,
        query: str,
        *,
        page: int = 1,
        page_size: int = 20,
        extra_filters: Mapping[str, Any] | None = None,
    ) -> tuple[list[RetrievalHit], int]:
        """Return one page of Explore results plus the exact match total.

        Empty queries are browsed directly from SQLite, so every card is
        reachable without loading the whole table.  Non-empty queries build a
        complete, stable ranked list once and cache it, which keeps page
        boundaries consistent while navigating.
        """
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        offset = (page - 1) * page_size
        intent = parse_intent(query)
        if intent.exact_id:
            hits = self._exact_hit(intent)
            return hits, len(hits)

        filters: dict[str, Any] = dict(intent.filters)
        if extra_filters:
            for key, value in extra_filters.items():
                if value is None or value == "":
                    continue
                if value == 0 and key not in (
                    "audit_hidden",
                    "audit_delete_suggested",
                    "audit_divergence",
                ):
                    continue
                filters[key] = value
        filters = self._with_default_audit_filter(filters, extra_filters)

        search_text = intent.residual or intent.raw
        if not search_text:
            total = self.db.count_knowledge_cards(filters=filters)
            cards = self.db.list_knowledge_cards(
                filters=filters,
                limit=page_size,
                offset=offset,
            )
            return [self._card_hit(card) for card in cards], total

        all_hits, total = self._all_hits(query, search_text, filters)
        return all_hits[offset : offset + page_size], total

    def clear_search_cache(self) -> None:
        self._page_cache.clear()

    @staticmethod
    def _with_default_audit_filter(
        filters: dict[str, Any],
        extra_filters: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Hide audit-suggested-delete cards unless a caller asks for them."""
        extra = dict(extra_filters or {})
        explicit = any(
            key in extra
            for key in (
                "audit_hidden",
                "audit_status",
                "audit_delete_suggested",
                "include_hidden",
            )
        )
        if not explicit and "audit_hidden" not in filters:
            filters["audit_hidden"] = 0
        return filters

    def _all_hits(
        self,
        query: str,
        search_text: str,
        filters: Mapping[str, Any],
    ) -> tuple[list[RetrievalHit], int]:
        key = (
            search_text,
            query,
            json.dumps(filters, sort_keys=True, default=str),
        )
        cached = self._page_cache.get(key)
        if cached is not None:
            self._page_cache.move_to_end(key)
            return cached

        fts_results, vector_results = self._collect_candidates(
            search_text,
            filters,
            fts_limit=self.MAX_PAGE_RESULTS,
            vector_limit=self.MAX_VECTOR_RESULTS,
        )
        hits = self._rank_candidates(
            query,
            search_text,
            fts_results,
            vector_results,
            max_candidates=self.MAX_PAGE_RESULTS,
            rerank_limit=self.RERANK_LIMIT,
            include_evidence=False,
        )
        result = (hits, len(hits))
        self._page_cache[key] = result
        self._page_cache.move_to_end(key)
        while len(self._page_cache) > self.PAGE_CACHE_SIZE:
            self._page_cache.popitem(last=False)
        return result

    def _collect_candidates(
        self,
        search_text: str,
        filters: Mapping[str, Any],
        *,
        fts_limit: int,
        vector_limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        fts_results: list[dict[str, Any]] = []
        if search_text:
            fts_results = self.db.search_cards_fts(
                search_text, filters=filters, limit=fts_limit
            )
        else:
            fts_results = self.db.list_knowledge_cards(
                filters=filters, limit=min(fts_limit, 1000)
            )

        vector_results: list[dict[str, Any]] = []
        if (
            search_text
            and self.qdrant is not None
            and self.qdrant.available
            and self.embedder is not None
            and getattr(self.embedder, "available", True)
        ):
            try:
                vector = self._embed(search_text)
                if vector:
                    # Older vectors do not yet carry audit_hidden in their
                    # payload; apply audit state after loading the SQLite rows.
                    vector_filters = {
                        key: value
                        for key, value in filters.items()
                        if key
                        not in (
                            "audit_hidden",
                            "audit_status",
                            "audit_delete_suggested",
                            "audit_divergence",
                            "source_kind",
                            "include_hidden",
                        )
                    }
                    raw = self.qdrant.search(
                        vector, filters=vector_filters, limit=vector_limit
                    )
                    ids = [item["knw_id"] for item in raw if item.get("knw_id")]
                    cards = {
                        card["knw_id"]: card
                        for card in self.db.get_knowledge_cards(ids)
                    }
                    for item in raw:
                        card = cards.get(item.get("knw_id"))
                        if card is None:
                            continue
                        if not self._card_matches_filters(card, filters):
                            continue
                        card = dict(card)
                        card["vector_score"] = float(item.get("score", 0.0))
                        vector_results.append(card)
            except Exception as exc:
                log.warning("Vector recall unavailable: %s", exc)
        return fts_results, vector_results

    def _rank_candidates(
        self,
        query: str,
        search_text: str,
        fts_results: Sequence[Mapping[str, Any]],
        vector_results: Sequence[Mapping[str, Any]],
        *,
        max_candidates: int,
        rerank_limit: int,
        include_evidence: bool,
    ) -> list[RetrievalHit]:
        candidates: dict[str, dict[str, Any]] = {}
        rrf_scores: dict[str, float] = {}
        for rank, card in enumerate(fts_results, start=1):
            knw_id = str(card["knw_id"])
            candidates.setdefault(knw_id, dict(card))
            rrf_scores[knw_id] = rrf_scores.get(knw_id, 0.0) + 1.0 / (
                self.rrf_k + rank
            )
        for rank, card in enumerate(vector_results, start=1):
            knw_id = str(card["knw_id"])
            candidates.setdefault(knw_id, dict(card))
            rrf_scores[knw_id] = rrf_scores.get(knw_id, 0.0) + 1.0 / (
                self.rrf_k + rank
            )
        if not candidates:
            return []

        ranked = sorted(
            candidates.values(),
            key=lambda card: rrf_scores.get(str(card["knw_id"]), 0.0),
            reverse=True,
        )[:max_candidates]
        # The local cross-encoder is fast for small result sets but scales
        # linearly; broad queries use the deterministic lexical score instead
        # of risking a multi-thousand-pair ONNX pass.
        rerank_scores = (
            self._rerank_scores(query, ranked)
            if len(ranked) <= max(1, int(rerank_limit))
            else None
        )
        now_year = datetime.now().year
        scored: list[tuple[float, int, dict[str, Any], dict[str, float]]] = []
        for index, card in enumerate(ranked):
            rerank = (
                rerank_scores[index]
                if rerank_scores is not None and index < len(rerank_scores)
                else self._lexical_score(search_text or query, card)
            )
            rerank = max(0.0, min(1.0, float(rerank)))
            consensus = min(
                1.0,
                math.log10(1 + max(1, int(card.get("consensus_count") or 1))) / 2.0,
            )
            authority = min(1.0, float(card.get("authority_score") or 1.0) / 1.5)
            recency_year = int(card.get("recency_year") or now_year)
            recency = max(0.0, min(1.0, (recency_year - 1990) / 36.0))
            final = (
                0.65 * rerank
                + 0.15 * consensus
                + 0.12 * authority
                + 0.08 * recency
            )
            pinned = 1 if card.get("card_level") == "L1" else 0
            if pinned:
                final += 0.03
            breakdown = {
                "rerank": rerank,
                "consensus": consensus,
                "authority": authority,
                "recency": recency,
                "rrf": rrf_scores.get(str(card["knw_id"]), 0.0),
                "final": final,
            }
            scored.append((final, pinned, card, breakdown))
        scored.sort(key=lambda item: (item[1], item[0]), reverse=True)

        hits: list[RetrievalHit] = []
        for final, _pinned, card, breakdown in scored:
            evidence = (
                self.db.list_evidence(card["knw_id"]) if include_evidence else []
            )
            hits.append(
                RetrievalHit(
                    kind="master" if card.get("card_level") == "L1" else "card",
                    id=card["knw_id"],
                    title=card.get("title_zh") or card.get("title_en") or "",
                    summary=card.get("summary_zh") or "",
                    score=final,
                    card=card,
                    snippet=card.get("snippet")
                    or card.get("summary_zh")
                    or card.get("search_payload", "")[:220],
                    evidence=evidence,
                    scores=breakdown,
                )
            )
        return hits

    @staticmethod
    def _card_hit(card: Mapping[str, Any]) -> RetrievalHit:
        return RetrievalHit(
            kind="master" if card.get("card_level") == "L1" else "card",
            id=str(card.get("knw_id") or ""),
            title=card.get("title_zh") or card.get("title_en") or "",
            summary=card.get("summary_zh") or "",
            score=0.0,
            card=dict(card),
            snippet=card.get("snippet")
            or card.get("summary_zh")
            or str(card.get("search_payload") or "")[:220],
            evidence=[],
        )

    def retrieve(self, query: str, **kwargs) -> list[RetrievalHit]:
        return self.search(query, **kwargs)

    # ------------------------------------------------------------------ exact
    def _exact_hit(self, intent: QueryIntent) -> list[RetrievalHit]:
        exact = intent.exact_id or {}
        family = exact.get("family")
        entity_id = exact.get("id", "")
        if family == "document":
            doc = self.db.get_document(entity_id)
            if not doc:
                return []
            return [
                RetrievalHit(
                    kind="document",
                    id=doc["doc_id"],
                    title=doc["title"],
                    summary=doc.get("abstract") or "",
                    score=1.0,
                    document=doc,
                    snippet=doc.get("abstract") or "",
                )
            ]
        if family in ("knowledge", "master"):
            card = self.db.get_knowledge_card(entity_id)
            if not card:
                return []
            return [
                RetrievalHit(
                    kind="master" if card.get("card_level") == "L1" else "card",
                    id=card["knw_id"],
                    title=card.get("title_zh") or card.get("title_en") or "",
                    summary=card.get("summary_zh") or "",
                    score=1.0,
                    card=card,
                    snippet=card.get("search_payload", "")[:240],
                    evidence=self.db.list_evidence(card["knw_id"]),
                )
            ]
        if family == "keyword":
            keyword = self.db.get_keyword(entity_id)
            if not keyword:
                return []
            return [
                RetrievalHit(
                    kind="keyword",
                    id=keyword["kw_id"],
                    title=keyword.get("term_en") or "",
                    summary=keyword.get("definition") or "",
                    score=1.0,
                    keyword=keyword,
                )
            ]
        if family == "evidence":
            evidence = self.db.get_evidence(entity_id)
            if not evidence:
                return []
            card = self.db.get_knowledge_card(evidence["parent_knw_id"])
            return [
                RetrievalHit(
                    kind="evidence",
                    id=evidence["chunk_id"],
                    title=evidence.get("section_title") or evidence["chunk_id"],
                    summary=evidence.get("context_bundle_md", "")[:500],
                    score=1.0,
                    card=card,
                    evidence=[evidence],
                )
            ]
        return []

    def lookup(self, entity_id: str) -> RetrievalHit | None:
        intent = QueryIntent(raw=entity_id, residual="", exact_id=classify_id(entity_id))
        if not intent.exact_id:
            return None
        hits = self._exact_hit(intent)
        return hits[0] if hits else None

    # ------------------------------------------------------------- similarity
    def _embed(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        if hasattr(self.embedder, "available") and not self.embedder.available:
            return None
        try:
            if hasattr(self.embedder, "embed_query"):
                return list(self.embedder.embed_query(text))
            if hasattr(self.embedder, "embed"):
                vectors = self.embedder.embed([text])
                return list(vectors[0]) if vectors else None
            if callable(self.embedder):
                return list(self.embedder(text))
        except Exception as exc:
            log.debug("Embedding probe failed: %s", exc)
        return None

    def _rerank_scores(
        self, query: str, cards: Sequence[Mapping[str, Any]]
    ) -> list[float] | None:
        if self.reranker is None or not cards:
            return None
        if hasattr(self.reranker, "available") and not self.reranker.available:
            return None
        documents = [
            str(card.get("search_payload") or card.get("summary_zh") or "")
            for card in cards
        ]
        try:
            if hasattr(self.reranker, "rerank"):
                scores = self.reranker.rerank(query, documents)
            elif callable(self.reranker):
                scores = self.reranker(query, documents)
            else:
                return None
            return [float(score) for score in scores]
        except Exception as exc:
            log.warning("Reranker failed; using lexical fallback: %s", exc)
            return None

    @staticmethod
    def _card_matches_filters(card: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
        for field in ("card_level", "card_type"):
            value = filters.get(field)
            if value not in (None, "") and str(card.get(field) or "").upper() != str(value).upper():
                return False
        for field in ("element", "isoelectronic", "ion_stage", "shell_type"):
            value = filters.get(field)
            if value not in (None, "") and str(card.get(field) or "").lower() != str(value).lower():
                return False
        if filters.get("z_num") not in (None, ""):
            try:
                if int(card.get("z_num") or -1) != int(filters["z_num"]):
                    return False
            except (ValueError, TypeError, OverflowError):
                return False
        for field in ("method", "physical_process", "code_name"):
            value = filters.get(field)
            if value not in (None, "") and str(value).lower() not in str(card.get(field) or "").lower():
                return False
        if filters.get("has_conflict") not in (None, "") and bool(card.get("has_conflict")) != bool(filters["has_conflict"]):
            return False
        if filters.get("conclusion_flags") not in (None, "", 0):
            if not (int(card.get("conclusion_flags") or 0) & int(filters["conclusion_flags"])):
                return False
        for field in ("audit_hidden", "audit_delete_suggested", "audit_divergence"):
            if filters.get(field) not in (None, ""):
                if bool(card.get(field)) != bool(filters[field]):
                    return False
        if filters.get("audit_status"):
            if str(card.get("audit_status") or "").upper() != str(
                filters["audit_status"]
            ).upper():
                return False
        if filters.get("source_kind"):
            if str(card.get("source_kind") or "").upper() != str(
                filters["source_kind"]
            ).upper():
                return False
        return True

    @staticmethod
    def _lexical_score(query: str, card: Mapping[str, Any]) -> float:
        query_tokens = set(re.findall(r"[A-Za-z0-9]+|[㐀-鿿]", query.lower()))
        if not query_tokens:
            return 0.0
        text = " ".join(
            str(card.get(key) or "")
            for key in ("title_zh", "title_en", "summary_zh", "search_payload")
        ).lower()
        doc_tokens = set(re.findall(r"[A-Za-z0-9]+|[㐀-鿿]", text))
        if not doc_tokens:
            return 0.0
        return len(query_tokens & doc_tokens) / len(query_tokens)


def build_intent(query: str) -> dict[str, Any]:
    return parse_intent(query).as_dict()
