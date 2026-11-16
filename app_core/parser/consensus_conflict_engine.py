"""Consensus clustering + polarity conflict radar.

The preferred path uses the bundled MiniLM (384-d) for fast graph clustering and
the NLI DeBERTa model for contradiction detection.  When those optional ONNX
files are absent, deterministic token cosine similarity and an antonym/negation
heuristic keep the pipeline functional on a bare CPU install.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[㐀-鿿]")
_NEGATION_RE = re.compile(r"\b(?:not|no|never|without|cannot|fail(?:s|ed)?)\b|不|未|无|非", re.I)

_ANTONYM_PAIRS = (
    ("increase", "decrease"),
    ("increased", "reduced"),
    ("increase", "reduce"),
    ("improve", "degrade"),
    ("improve", "worsen"),
    ("agree", "disagree"),
    ("agreement", "discrepancy"),
    ("converge", "diverge"),
    ("converged", "failed"),
    ("overestimate", "underestimate"),
    ("higher", "lower"),
    ("stronger", "weaker"),
    ("enhanced", "reduced"),
    ("增大", "减小"),
    ("提高", "降低"),
    ("一致", "不一致"),
    ("收敛", "发散"),
    ("优于", "劣于"),
    ("偏高", "偏低"),
)


@dataclass
class ConflictResult:
    conflict: bool
    score: float = 0.0
    reason: str = ""
    method: str = "heuristic"
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConsolidationResult:
    duplicate: bool = False
    target_id: str = ""
    similarity: float = 0.0
    consensus_count: int = 1
    supporting_doc_ids: list[str] = field(default_factory=list)
    conflict_ids: list[str] = field(default_factory=list)
    has_conflict: bool = False
    reasons: list[str] = field(default_factory=list)


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(str(text or ""))}


def _token_cosine(a: str, b: str) -> float:
    left = _tokens(a)
    right = _tokens(b)
    if not left or not right:
        return 0.0
    common = left & right
    return len(common) / math.sqrt(len(left) * len(right))


def _cosine(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(x * x for x in vec_a))
    norm_b = math.sqrt(sum(y * y for y in vec_b))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _card_text(card: Mapping[str, Any]) -> str:
    return " ".join(
        str(card.get(key) or "")
        for key in (
            "title_zh",
            "title_en",
            "summary_zh",
            "premises_and_conditions",
            "search_payload",
        )
    )


class ConsensusConflictEngine:
    def __init__(
        self,
        *,
        registry: Any = None,
        duplicate_threshold: float = 0.82,
        conflict_threshold: float = 0.6,
    ):
        self.registry = registry
        self.duplicate_threshold = duplicate_threshold
        self.conflict_threshold = conflict_threshold

    # ------------------------------------------------------------- similarity
    def similarity(self, text_a: str, text_b: str) -> float:
        if self.registry is not None and self.registry.is_present("mini_cluster_embed"):
            try:
                vectors = self.registry.embed_minilm([text_a, text_b])
                return max(0.0, min(1.0, _cosine(vectors[0], vectors[1])))
            except Exception as exc:
                log.debug("MiniLM similarity failed, falling back: %s", exc)
        return max(0.0, min(1.0, _token_cosine(text_a, text_b)))

    def cluster(
        self,
        texts: Sequence[str],
        *,
        threshold: float | None = None,
        progress: Callable[[str, float], None] | None = None,
    ) -> list[list[int]]:
        threshold = self.duplicate_threshold if threshold is None else threshold
        count = len(texts)

        def report(label: str, fraction: float) -> None:
            if progress is not None:
                try:
                    progress(label, max(0.0, min(1.0, float(fraction))))
                except Exception:
                    log.debug("Cluster progress callback failed", exc_info=True)
        parent = list(range(count))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(a: int, b: int) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        # Optional MiniLM batch gives a much better graph than pairwise token
        # cosine; batch once and reuse.
        vectors: list[Sequence[float]] | None = None
        if self.registry is not None and self.registry.is_present("mini_cluster_embed"):
            try:
                vectors = self.registry.embed_minilm(
                    list(texts),
                    progress=(
                        lambda done, total: report(
                            f"MiniLM 语义编码 {done}/{total}",
                            0.02 + 0.70 * done / max(1, total),
                        )
                    ) if progress else None,
                )
            except Exception as exc:
                log.debug("MiniLM clustering unavailable: %s", exc)
        if vectors is not None:
            import numpy as np

            matrix = np.asarray(vectors, dtype=np.float32)
            if matrix.ndim == 2 and matrix.shape[0] == count:
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                matrix = matrix / np.clip(norms, 1e-9, None)
                block = 256
                for start in range(0, count, block):
                    stop = min(count, start + block)
                    scores = matrix[start:stop] @ matrix.T
                    for local_i, j in np.argwhere(scores >= threshold):
                        i = start + int(local_i)
                        j = int(j)
                        if i < j:
                            union(i, j)
                    report(
                        f"相似度分块 {stop}/{count}",
                        0.74 + 0.22 * stop / max(1, count),
                    )
            else:
                vectors = None
        if vectors is None:
            for i in range(count):
                if i % 100 == 0:
                    report(
                        f"启发式相似度 {i}/{count}",
                        0.05 + 0.85 * i / max(1, count),
                    )
                for j in range(i + 1, count):
                    if _token_cosine(texts[i], texts[j]) >= threshold:
                        union(i, j)
        groups: dict[int, list[int]] = {}
        for index in range(count):
            groups.setdefault(find(index), []).append(index)
        report(f"聚类完成：{len(groups)} 组", 1.0)
        return list(groups.values())

    def find_duplicates(
        self, text: str, candidates: Sequence[tuple[str, str]]
    ) -> list[tuple[str, float]]:
        """``candidates`` is a sequence of ``(id, text)``; returns id/score."""
        matches: list[tuple[str, float]] = []
        for candidate_id, candidate_text in candidates:
            score = self.similarity(text, candidate_text)
            if score >= self.duplicate_threshold:
                matches.append((candidate_id, score))
        return sorted(matches, key=lambda item: item[1], reverse=True)

    # --------------------------------------------------------------- conflict
    def detect_conflict(self, text_a: str, text_b: str) -> ConflictResult:
        if self.registry is not None and self.registry.is_present("conflict_nli"):
            try:
                probabilities = self.registry.nli_local(text_a, text_b)
                contradiction = float(probabilities.get("contradiction", 0.0))
                return ConflictResult(
                    conflict=contradiction >= self.conflict_threshold,
                    score=contradiction,
                    reason=f"NLI contradiction={contradiction:.3f}",
                    method="nli",
                    details=probabilities,
                )
            except Exception as exc:
                log.debug("NLI conflict detection failed, falling back: %s", exc)

        score, reason = self._heuristic_conflict(text_a, text_b)
        return ConflictResult(
            conflict=score >= self.conflict_threshold,
            score=score,
            reason=reason,
            method="heuristic",
        )

    def detect_conflict_batch(
        self, pairs: Sequence[tuple[str, str]]
    ) -> list[ConflictResult]:
        """Batch NLI for the full-library conflict pass.

        DeBERTa inference dominates Stage 5 on a 3,648-card library.  Batched
        pairs use the same tokenizer and ONNX session but far fewer runtime
        calls.  If the model is unavailable, the deterministic heuristic is
        used for each pair.
        """
        values = list(pairs)
        if not values:
            return []
        if self.registry is not None and self.registry.is_present("conflict_nli"):
            try:
                probabilities = self.registry.nli_batch(values)
                if len(probabilities) == len(values):
                    results: list[ConflictResult] = []
                    for item in probabilities:
                        contradiction = float(item.get("contradiction", 0.0))
                        results.append(
                            ConflictResult(
                                conflict=contradiction >= self.conflict_threshold,
                                score=contradiction,
                                reason=f"NLI contradiction={contradiction:.3f}",
                                method="nli",
                                details=item,
                            )
                        )
                    return results
            except Exception as exc:
                log.debug("Batched NLI conflict detection failed, falling back: %s", exc)
        return [
            self.detect_conflict(text_a, text_b)
            for text_a, text_b in values
        ]

    @staticmethod
    def _heuristic_conflict(text_a: str, text_b: str) -> tuple[float, str]:
        left = str(text_a or "").lower()
        right = str(text_b or "").lower()
        base = _token_cosine(left, right)
        if base < 0.10:
            return 0.0, "主题重叠不足"
        reasons: list[str] = []
        score = base * 0.45

        for positive, negative in _ANTONYM_PAIRS:
            if (positive in left and negative in right) or (
                negative in left and positive in right
            ):
                reasons.append(f"对立词 {positive}/{negative}")
                score = max(score, 0.72)
                break

        left_neg = bool(_NEGATION_RE.search(left))
        right_neg = bool(_NEGATION_RE.search(right))
        if left_neg != right_neg:
            reasons.append("否定极性不一致")
            score = max(score, 0.66)

        if re.search(r"discrepan|disagree|inconsisten|contradict", left + " " + right):
            if not re.search(r"agree|consistent|excellent", left + " " + right):
                reasons.append("显式不一致/矛盾措辞")
                score = max(score, 0.7)

        return min(1.0, score), "; ".join(reasons) or "启发式相似度"

    # ---------------------------------------------------------- consolidation
    def consolidate(
        self,
        new_card: Mapping[str, Any],
        existing_cards: Sequence[Mapping[str, Any]],
    ) -> ConsolidationResult:
        text = _card_text(new_card)
        candidates = [(str(card.get("knw_id")), _card_text(card)) for card in existing_cards]
        duplicates = self.find_duplicates(text, candidates)
        result = ConsolidationResult(
            consensus_count=int(new_card.get("consensus_count") or 1),
            supporting_doc_ids=list(new_card.get("supporting_doc_ids") or []),
        )
        for card in existing_cards:
            other_text = _card_text(card)
            conflict = self.detect_conflict(text, other_text)
            if conflict.conflict:
                result.has_conflict = True
                knw_id = str(card.get("knw_id"))
                if knw_id and knw_id not in result.conflict_ids:
                    result.conflict_ids.append(knw_id)
                result.reasons.append(f"冲突 {knw_id}: {conflict.reason}")
        if duplicates:
            target_id, score = duplicates[0]
            target = next(
                (card for card in existing_cards if str(card.get("knw_id")) == target_id),
                None,
            )
            result.duplicate = True
            result.target_id = target_id
            result.similarity = score
            result.consensus_count = int(
                (target or {}).get("consensus_count", 1)
            ) + int(new_card.get("consensus_count", 1) or 1)
            docs = set((target or {}).get("supporting_doc_ids_list") or [])
            docs.update(new_card.get("supporting_doc_ids") or [])
            result.supporting_doc_ids = sorted(docs)
            result.reasons.append(f"与 {target_id} 同义合并 ({score:.3f})")
        return result
