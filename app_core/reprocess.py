"""Token-aware reprocessing and local repair for the document library.

Three levels are exposed to the GUI:

* ``local_clean`` — metadata/filename repair, bad-card cleanup, formula
  sanitising and local ONNX reclustering (no LLM calls);
* ``incremental`` — parse candidates again but skip every bundle whose hash is
  already attached to a valid card;
* ``force`` — delete unverified cards for selected documents and send every
  candidate bundle to the fast model again, preserving user-verified cards.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

from parser.card_quality import purge_invalid_cards, sanitize_existing_cards
from parser.context_bundler import ContextBundle, bundle_hash, bundle_hash_from_text
from parser.document_profile import is_low_value_bundle, is_manual_document
from parser.metadata_extractor import repair_invalid_documents

log = logging.getLogger(__name__)

Progress = Any
# Each extraction request repeats the system prompt and JSON schema; the
# bundle text alone substantially underestimates input-token usage.
_INPUT_OVERHEAD_PER_BUNDLE = 2000


class ReprocessManager:
    def __init__(self, db: Any, workspace: str | Path, pipeline: Any):
        self.db = db
        self.workspace = Path(workspace)
        self.pipeline = pipeline
        self.qdrant = getattr(pipeline, "qdrant", None)
        self.gateway = getattr(pipeline, "gateway", None)

    # --------------------------------------------------------------- tier 1
    def _refresh_services(self) -> None:
        self.gateway = getattr(self.pipeline, "gateway", self.gateway)
        self.qdrant = getattr(self.pipeline, "qdrant", self.qdrant)

    def local_clean(
        self,
        *,
        operations: Iterable[str] | None = None,
        doc_ids: Iterable[str] | None = None,
        progress: Progress = None,
        should_stop: Any = None,
    ) -> dict[str, Any]:
        self._refresh_services()
        self.db.backfill_bundle_hashes(bundle_hash_from_text)
        op_set = (
            {"metadata", "purge", "formulas", "recluster", "conflicts"}
            if operations is None
            else {str(op).lower() for op in operations}
        )

        metadata = {"checked": 0, "repaired": 0, "failed": 0}
        purged = {"checked": 0, "deleted": 0}
        formulas = {"checked": 0, "updated": 0}
        recluster = {"checked": 0, "merged": 0, "conflicts": 0}

        if "metadata" in op_set:
            if should_stop and should_stop():
                return {"metadata": metadata, "purged": purged, "formulas": formulas, "recluster": recluster, "stopped": True}
            if progress:
                progress("阶段 1/5 · 扫描并修复文献元数据与 Zotero 文件名", 0.02)
            metadata = repair_invalid_documents(
                self.db,
                self.workspace,
                limit=2000,
                allow_network=False,
                include_valid=True,
                doc_ids=doc_ids,
                should_stop=should_stop,
                progress=lambda index, total, doc_id: progress(
                    f"阶段 1/5 · 修复文献元数据 {index}/{total} · {doc_id}",
                    0.03 + 0.29 * index / max(1, total),
                ) if progress else None,
            )

        if "purge" in op_set:
            if should_stop and should_stop():
                return {"metadata": metadata, "purged": purged, "formulas": formulas, "recluster": recluster, "stopped": True}
            if progress:
                progress("阶段 2/5 · 清理证据截断废卡", 0.33)
            purged = purge_invalid_cards(
                self.db,
                self.qdrant,
                doc_ids=doc_ids,
                should_stop=should_stop,
                progress=lambda done, total: progress(
                    f"阶段 2/5 · 清理证据截断废卡 {done}/{total}",
                    0.33 + 0.04 * done / max(1, total),
                ) if progress else None,
            )

        if "formulas" in op_set:
            if should_stop and should_stop():
                return {"metadata": metadata, "purged": purged, "formulas": formulas, "recluster": recluster, "stopped": True}
            if progress:
                progress("阶段 3/5 · 净化公式伪影与说明文字", 0.38)
            formulas = sanitize_existing_cards(
                self.db,
                self.qdrant,
                doc_ids=doc_ids,
                should_stop=should_stop,
                progress=lambda done, total: progress(
                    f"阶段 3/5 · 净化公式伪影与说明文字 {done}/{total}",
                    0.38 + 0.05 * done / max(1, total),
                ) if progress else None,
            )

        if "recluster" in op_set or "conflicts" in op_set:
            if should_stop and should_stop():
                return {"metadata": metadata, "purged": purged, "formulas": formulas, "recluster": recluster, "stopped": True}
            if progress:
                progress("阶段 4/5 · 准备本地 ONNX 聚类与争议校验", 0.44)
            # Free the BGE-M3 session before loading MiniLM/NLI. On a full library
            # both sessions plus the ONNX arena can push an 8 GB machine to OOM.
            gateway = getattr(self.pipeline, "gateway", None)
            embedding = getattr(gateway, "embedding", None)
            if hasattr(embedding, "release_local_session"):
                try:
                    embedding.release_local_session()
                except Exception:
                    log.debug("BGE session release failed", exc_info=True)
            recluster = self._recluster_local(
                doc_ids=doc_ids,
                should_stop=should_stop,
                skip_conflicts="conflicts" not in op_set,
                progress=progress,
            )

        result = {
            "metadata": metadata,
            "purged": purged,
            "formulas": formulas,
            "recluster": recluster,
            "stopped": bool(should_stop and should_stop()),
        }
        self.db.log_event(
            "INFO",
            "reprocess",
            f"本地修复：元数据 {metadata.get('repaired', 0)} 篇，"
            f"删除废卡 {purged.get('deleted', 0)} 张，"
            f"公式净化 {formulas.get('updated', 0)} 张，"
            f"聚类合并 {recluster.get('merged', 0)} 张",
        )
        if progress:
            if result.get("stopped"):
                progress("⚠️ 用户已安全中止，已处理数据已完整落盘", 1.0)
            else:
                progress("✅ 本地清洗完成", 1.0)
        return result

    def _recluster_local(
        self,
        *,
        doc_ids: Iterable[str] | None = None,
        should_stop: Any = None,
        skip_conflicts: bool = False,
        progress: Progress = None,
    ) -> dict[str, int]:
        engine = getattr(self.pipeline, "consensus", None)
        if engine is None:
            return {"checked": 0, "merged": 0, "conflicts": 0}

        def emit(label: str, fraction: float) -> None:
            if progress:
                progress(label, max(0.0, min(1.0, float(fraction))))

        cards: list[dict[str, Any]] = []
        if doc_ids is not None:
            clean_doc_ids = set(str(did).strip().upper() for did in doc_ids if str(did).strip())
            for did in clean_doc_ids:
                cards.extend(
                    card for card in self.db.list_knowledge_cards(doc_id=did, limit=5000)
                    if str(card.get("card_level") or "L2").upper() == "L2"
                )
        else:
            total_cards = self.db.count_knowledge_cards()
            offset = 0
            while True:
                if should_stop and should_stop():
                    return {"checked": len(cards), "merged": 0, "conflicts": 0}
                batch = self.db.list_knowledge_cards(limit=500, offset=offset)
                if not batch:
                    break
                cards.extend(
                    card for card in batch
                    if str(card.get("card_level") or "L2").upper() == "L2"
                )
                offset += len(batch)
                emit(
                    f"阶段 4/5 · 加载知识卡 {min(offset, total_cards)}/{total_cards}",
                    0.44 + 0.02 * min(offset, total_cards) / max(1, total_cards),
                )
                if len(batch) < 500:
                    break
        if len(cards) < 2:
            emit("阶段 5/5 · 聚类完成：卡片不足", 0.99)
            return {"checked": len(cards), "merged": 0, "conflicts": 0}

        def text(card: dict[str, Any]) -> str:
            return str(
                card.get("search_payload")
                or " ".join(
                    str(card.get(key) or "")
                    for key in ("title_zh", "summary_zh", "premises_and_conditions")
                )
            )

        def conflict_text(card: dict[str, Any]) -> str:
            # NLI only needs the claim and its conditions.  Sending the full
            # search_payload (often ~512 tokens) makes each DeBERTa call much
            # slower and adds retrieval noise.
            claim = " ".join(
                str(card.get(key) or "")
                for key in ("title_zh", "summary_zh", "premises_and_conditions")
            ).strip()
            return (claim or text(card))[:2000]

        emit(f"阶段 4/5 · MiniLM 语义编码准备 {len(cards)} 张卡", 0.47)
        groups = engine.cluster(
            [text(card) for card in cards],
            progress=lambda label, fraction: emit(
                f"阶段 4/5 · {label}", 0.47 + 0.34 * fraction
            ),
        )
        def pick_primary(members: list[dict[str, Any]]) -> dict[str, Any]:
            verified = [
                card for card in members
                if int(card.get("is_user_verified") or 0)
            ]
            return max(
                verified or members,
                key=lambda item: (
                    int(item.get("is_user_verified") or 0),
                    int(item.get("consensus_count") or 1),
                    float(item.get("authority_score") or 1.0),
                ),
            )

        merged = 0
        conflicts = 0
        total_groups = len(groups)
        # Small duplicate groups get a full pairwise NLI check.  Large groups
        # are compared against their strongest card instead of all O(n²)
        # pairs: a 100-card group would otherwise issue thousands of expensive
        # DeBERTa inferences and freeze the UI at 81%.
        total_pair_checks = sum(
            len(group) - 1
            if len(group) > 8
            else len(group) * (len(group) - 1) // 2
            for group in groups
        )
        total_work = total_pair_checks + total_groups
        checked_pairs = 0
        checked_groups = 0

        def report_group_work() -> None:
            done = checked_pairs + checked_groups
            detail = f"阶段 5/5 · 争议校验 {checked_pairs}/{total_pair_checks} 对"
            if checked_groups:
                detail += f" · 已合并 {merged} · 争议 {conflicts} 对"
            emit(
                detail,
                min(0.99, 0.82 + 0.17 * done / max(1, total_work)),
            )

        group_members = [[cards[index] for index in group] for group in groups]
        group_pairs: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []
        flat_pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for members in group_members:
            if len(members) <= 1:
                pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
            elif len(members) <= 8:
                pairs = [
                    (members[i], members[j])
                    for i in range(len(members))
                    for j in range(i + 1, len(members))
                ]
            else:
                anchor = pick_primary(members)
                pairs = [
                    (anchor, member)
                    for member in members
                    if member is not anchor
                ]
            group_pairs.append(pairs)
            flat_pairs.extend(pairs)

        emit("阶段 5/5 · 准备 NLI / 启发式争议校验", 0.82)
        verdicts: list[Any] = []
        if skip_conflicts:
            flat_pairs = []
        batch_detect = getattr(engine, "detect_conflict_batch", None)
        # NLI is batched here, so 2,514 pair checks do not become 2,514
        # separate ONNX session calls.  The progress label still advances by
        # the number of checked pairs.
        for start in range(0, len(flat_pairs), 32):
            if should_stop and should_stop():
                break
            chunk = flat_pairs[start:start + 32]
            chunk_texts = [
                (conflict_text(left), conflict_text(right))
                for left, right in chunk
            ]
            if callable(batch_detect):
                verdicts.extend(batch_detect(chunk_texts))
            else:
                verdicts.extend(
                    engine.detect_conflict(left, right)
                    for left, right in chunk_texts
                )
            checked_pairs = len(verdicts)
            report_group_work()
        if not flat_pairs:
            report_group_work()

        pair_offset = 0
        for members, pairs in zip(group_members, group_pairs):
            if should_stop and should_stop():
                break
            group_verdicts = verdicts[pair_offset:pair_offset + len(pairs)]
            pair_offset += len(pairs)
            conflict_ids: set[str] = set()
            if len(members) >= 2:
                for (left, right), verdict in zip(pairs, group_verdicts):
                    if not verdict.conflict:
                        continue
                    conflicts += 1
                    left_id = str(left.get("knw_id"))
                    right_id = str(right.get("knw_id"))
                    conflict_ids.update((left_id, right_id))
                    for item, other_id in ((left, right_id), (right, left_id)):
                        current = set(item.get("conflict_with_knw_ids_list") or [])
                        current.add(other_id)
                        self.db.upsert_knowledge_card(
                            {
                                "knw_id": item["knw_id"],
                                "has_conflict": 1,
                                "conflict_with_knw_ids": sorted(current),
                            }
                        )
                if conflict_ids:
                    checked_groups += 1
                    if checked_groups == 1 or checked_groups % 10 == 0:
                        report_group_work()
                    continue
                primary = pick_primary(members)
                docs = set(primary.get("supporting_doc_ids_list") or [])
                consensus = int(primary.get("consensus_count") or 1)
                for member in members:
                    docs.update(member.get("supporting_doc_ids_list") or [])
                    if member is not primary:
                        consensus += int(member.get("consensus_count") or 1)
                self.db.upsert_knowledge_card(
                    {
                        "knw_id": primary["knw_id"],
                        "supporting_doc_ids": sorted(docs),
                        "consensus_count": consensus,
                    }
                )
                for member in members:
                    if member is primary or int(member.get("is_user_verified") or 0):
                        continue
                    if self.db.delete_knowledge_card(str(member["knw_id"])):
                        merged += 1
                        if self.qdrant is not None:
                            try:
                                self.qdrant.delete(str(member["knw_id"]))
                            except Exception:
                                pass
            checked_groups += 1
            if checked_groups == 1 or checked_groups % 10 == 0 or checked_groups == total_groups:
                report_group_work()
        registry = getattr(self.gateway, "registry", None)
        method = "ONNX MiniLM/NLI" if registry is not None else "本地启发式"
        self.db.log_event(
            "INFO",
            "reprocess",
            f"本地聚类校验（{method}）：检查 {len(cards)} 张，合并 {merged} 张，"
            f"争议 {conflicts} 对",
        )
        emit(f"阶段 5/5 · 聚类完成：检查 {len(cards)} 张，合并 {merged} 张", 0.99)
        return {"checked": len(cards), "merged": merged, "conflicts": conflicts}

    # --------------------------------------------------------------- tier 2
    def incremental(
        self, doc_ids: Iterable[str] | None = None, *, progress: Progress = None,
        force_manual: bool | None = None, should_stop: Any = None,
    ) -> dict[str, Any]:
        self._refresh_services()
        self.db.backfill_bundle_hashes(bundle_hash_from_text)
        docs = self._documents(doc_ids)
        total = len(docs)
        stats: dict[str, Any] = {
            "documents": 0, "extracted": 0, "skipped": 0,
            "skipped_low_value": 0, "failed": 0, "with_cards": 0,
            "cancelled": False,
        }
        for index, document in enumerate(docs, start=1):
            if should_stop is not None and should_stop():
                stats["cancelled"] = True
                break
            doc_id = str(document["doc_id"])
            start = 0.05 + 0.9 * (index - 1) / max(1, total)
            span = 0.9 / max(1, total)
            if progress:
                progress(f"增量处理 {index}/{total} · {doc_id}", start)
            parsed = self.pipeline.parse_document(doc_id, force_manual=force_manual)
            if parsed is None:
                stats["failed"] += 1
                continue
            if should_stop is not None and should_stop():
                stats["cancelled"] = True
                break
            _parsed, bundles, _path = parsed
            result = self.pipeline.extract_bundles(
                doc_id, bundles, force=False, should_stop=should_stop,
                progress=lambda label, fraction, i=index, base=start, width=span: progress(
                    f"{label} · 文献 {i}/{total}",
                    base + width * max(0.0, min(1.0, float(fraction))),
                ) if progress else None,
            )
            stats["documents"] += 1
            stats["extracted"] += int(result.get("extracted", 0))
            stats["skipped"] += int(result.get("skipped_existing", 0))
            stats["skipped_low_value"] += int(result.get("skipped_low_value", 0))
            stats["failed"] += int(result.get("failed", 0))
            if result.get("cards", 0):
                stats["with_cards"] += 1
            if result.get("cancelled"):
                stats["cancelled"] = True
                break
        if progress:
            progress("智能增量补提已取消" if stats["cancelled"] else "智能增量补提完成", 1.0)
        self.db.log_event(
            "WARNING" if stats["cancelled"] else "INFO",
            "reprocess",
            f"智能增量补提：文献 {stats['documents']} 篇，新卡 {stats['extracted']} 张，"
            f"跳过已提炼片段 {stats['skipped']} 个，"
            f"过滤低价值片段 {stats['skipped_low_value']} 个"
            + ("；用户已取消" if stats["cancelled"] else ""),
        )
        return stats

    # --------------------------------------------------------------- tier 3
    def estimate_force_tokens(self, doc_ids: Iterable[str]) -> int:
        chars = 0
        bundles = 0
        for document in self._documents(doc_ids):
            bundles_path = self._bundles_path(document)
            if bundles_path is not None and bundles_path.is_file():
                try:
                    raw = json.loads(bundles_path.read_text(encoding="utf-8"))
                    chars += sum(
                        len(json.dumps(item, ensure_ascii=False)) for item in raw
                    )
                    bundles += len(raw)
                    continue
                except (OSError, ValueError):
                    pass
            md_path = document.get("local_md_path")
            if md_path:
                path = Path(str(md_path))
                if not path.is_absolute():
                    path = self.workspace / path
                if path.is_file():
                    try:
                        chars += len(path.read_text(encoding="utf-8"))
                        bundles += 1
                    except OSError:
                        pass
        return max(
            1,
            int(chars / 3.5) + bundles * _INPUT_OVERHEAD_PER_BUNDLE,
        )

    def estimate_incremental_tokens(self, doc_ids: Iterable[str] | None = None) -> int:
        """Estimate only the bundles that are still missing from the cache."""
        config = getattr(self.pipeline, "config", None)
        skip_low_value = True
        if config is not None and hasattr(config, "get"):
            try:
                skip_low_value = bool(
                    config.get("extraction.skip_low_value_manual", True)
                )
            except Exception:
                pass
        chars = 0
        bundles = 0
        for document in self._documents(doc_ids):
            doc_id = str(document["doc_id"])
            stored = self.db.bundle_hashes_for_document(doc_id)
            manual = is_manual_document(
                title=str(document.get("title") or ""),
                doc_type=str(document.get("doc_type") or ""),
                filename=Path(str(document.get("local_pdf_path") or "")).name,
            )
            bundles_path = self._bundles_path(document)
            if bundles_path is not None and bundles_path.is_file():
                try:
                    raw = json.loads(bundles_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raw = []
                for item in raw:
                    try:
                        bundle = ContextBundle.model_validate(item)
                        digest = bundle_hash(bundle)
                    except Exception:
                        continue
                    if digest in stored:
                        continue
                    if manual and skip_low_value and is_low_value_bundle(
                        bundle.section_path,
                        bundle.focal_window_markdown,
                        bundle.focal_sentence,
                        manual=True,
                    ):
                        continue
                    chars += len(json.dumps(item, ensure_ascii=False))
                    bundles += 1
                continue
            md_path = document.get("local_md_path")
            if md_path:
                path = Path(str(md_path))
                if not path.is_absolute():
                    path = self.workspace / path
                if path.is_file():
                    try:
                        chars += len(path.read_text(encoding="utf-8"))
                        bundles += 1
                    except OSError:
                        pass
        return max(
            0,
            int(chars / 3.5) + bundles * _INPUT_OVERHEAD_PER_BUNDLE,
        )

    def force(
        self, doc_ids: Iterable[str], *, progress: Progress = None,
        force_manual: bool | None = None, should_stop: Any = None,
    ) -> dict[str, Any]:
        self._refresh_services()
        docs = self._documents(doc_ids)
        total = len(docs)
        stats: dict[str, Any] = {
            "documents": 0, "extracted": 0, "skipped": 0,
            "skipped_low_value": 0, "failed": 0, "with_cards": 0,
            "cancelled": False,
        }
        for index, document in enumerate(docs, start=1):
            if should_stop is not None and should_stop():
                stats["cancelled"] = True
                break
            doc_id = str(document["doc_id"])
            start = 0.05 + 0.9 * (index - 1) / max(1, total)
            span = 0.9 / max(1, total)
            if progress:
                progress(f"强制重提 {index}/{total} · {doc_id}", start)
            parsed = self.pipeline.parse_document(doc_id, force_manual=force_manual)
            if parsed is None:
                stats["failed"] += 1
                continue
            if should_stop is not None and should_stop():
                stats["cancelled"] = True
                break
            _parsed, bundles, _path = parsed
            result = self.pipeline.extract_bundles(
                doc_id, bundles, force=True, should_stop=should_stop,
                progress=lambda label, fraction, i=index, base=start, width=span: progress(
                    f"{label} · 文献 {i}/{total}",
                    base + width * max(0.0, min(1.0, float(fraction))),
                ) if progress else None,
            )
            stats["documents"] += 1
            stats["extracted"] += int(result.get("extracted", 0))
            stats["skipped_low_value"] += int(result.get("skipped_low_value", 0))
            stats["failed"] += int(result.get("failed", 0))
            if result.get("cards", 0):
                stats["with_cards"] += 1
            if result.get("cancelled"):
                stats["cancelled"] = True
                break
        if progress:
            progress("强制重提已取消" if stats["cancelled"] else "强制重提完成", 1.0)
        self.db.log_event(
            "WARNING" if stats["cancelled"] else "INFO",
            "reprocess",
            f"强制重提：文献 {stats['documents']} 篇，新卡 {stats['extracted']} 张，"
            f"过滤低价值片段 {stats['skipped_low_value']} 个"
            + ("；用户已取消" if stats["cancelled"] else ""),
        )
        return stats

    # --------------------------------------------------------------- helpers
    def _documents(self, doc_ids: Iterable[str] | None) -> list[dict[str, Any]]:
        if doc_ids is None:
            return self.db.list_documents(limit=2000)
        values = [str(item).upper() for item in doc_ids if item]
        result = []
        for doc_id in values:
            document = self.db.get_document(doc_id)
            if document:
                result.append(document)
        return result

    def _bundles_path(self, document: dict[str, Any]) -> Path | None:
        value = document.get("local_pdf_path")
        if not value:
            return None
        path = Path(str(value))
        if not path.is_absolute():
            path = self.workspace / path
        return path.parent / "bundles.json"


__all__ = ["ReprocessManager", "bundle_hash_from_text", "ContextBundle"]
