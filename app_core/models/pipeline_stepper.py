"""Pipeline Stepper — Document Processing 5-Step State Machine & Cascading Rollback.

Workflow Steps:
  1. TXT_EXTRACT       (提取文字)
  2. INFO_EXTRACT      (提取关键信息)
  3. AI_FAST_REVIEW    (AI 快速提取)
  4. DB_WRITE          (写入数据库)   -> 冒绿光 (Green Glow)
  5. AI_DEEP_SYNTHESIS (AI 深度归纳) -> 冒金光 (Gold Glow)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class StepKey(str, Enum):
    TXT_EXTRACT = "txt_extract"
    INFO_EXTRACT = "info_extract"
    AI_FAST = "ai_fast"
    DB_WRITE = "db_write"
    AI_DEEP = "ai_deep"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class GlowState(str, Enum):
    NONE = "NONE"
    GREEN = "GREEN"  # Step 4 complete (basic data ingested & ready)
    GOLD = "GOLD"    # Step 5 complete (deep synthesis & master card ready)


STEP_DEFINITIONS = [
    (1, StepKey.TXT_EXTRACT, "提取文字", "PDF/CAJ 文本层提取、OCR 识别与排版结构化"),
    (2, StepKey.INFO_EXTRACT, "提取关键信息", "4 通道雷达扫描、图表公式定位与证据包 (ContextBundle) 生成"),
    (3, StepKey.AI_FAST, "AI 快速提取", "初版原子化 L2 细节知识卡提炼 (支持 API / 网页半自动)"),
    (4, StepKey.DB_WRITE, "写入数据库", "校验卡片、事务入库、检索索引与向量化存储 (及格线·冒绿光)"),
    (5, StepKey.AI_DEEP, "AI 深度归纳", "跨卡综合归纳为顶层 L1 Master 主卡片与争议裁决 (彻底吃透·冒金光)"),
]


@dataclass
class StepInfo:
    index: int
    key: str
    name: str
    status: StepStatus = StepStatus.PENDING
    description: str = ""
    error_message: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "key": self.key,
            "name": self.name,
            "status": self.status.value,
            "description": self.description,
            "error_message": self.error_message,
            "updated_at": self.updated_at,
        }


@dataclass
class DocumentPipelineStatus:
    doc_id: str
    steps: list[StepInfo] = field(default_factory=list)
    glow: GlowState = GlowState.NONE
    card_count: int = 0
    has_master: bool = False
    current_step: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "steps": [s.to_dict() for s in self.steps],
            "glow": self.glow.value,
            "card_count": self.card_count,
            "has_master": self.has_master,
            "current_step": self.current_step,
        }


def compute_document_pipeline_status(
    doc: dict[str, Any],
    cards: list[dict[str, Any]] | None = None,
    *,
    card_count: int | None = None,
    has_master: bool | None = None,
) -> DocumentPipelineStatus:
    """Compute the 5-step status and glow state dynamically from document & cards."""
    doc_id = str(doc.get("doc_id") or "")
    if card_count is None:
        cards_list = list(cards or [])
        count_val = len(cards_list)
        master_val = any(
            str(c.get("card_type") or "").upper() == "MASTER"
            or str(c.get("card_level") or "").upper() == "L1"
            for c in cards_list
        )
    else:
        count_val = int(card_count)
        master_val = bool(has_master)

    card_count = count_val
    has_master = master_val

    parse_status = str(doc.get("parse_status") or "PENDING").upper()
    download_status = str(doc.get("download_status") or "QUEUED").upper()
    error_code = str(doc.get("parse_error_code") or "")
    error_log = str(doc.get("error_log") or "")

    steps: list[StepInfo] = []
    for idx, key, name, default_desc in STEP_DEFINITIONS:
        steps.append(
            StepInfo(
                index=idx,
                key=key.value,
                name=name,
                status=StepStatus.PENDING,
                description=default_desc,
            )
        )

    glow = GlowState.NONE

    bundles_exist = False
    local_pdf = doc.get("local_pdf_path")
    if local_pdf:
        try:
            bundles_path = Path(local_pdf).parent / "bundles.json"
            bundles_exist = bundles_path.is_file() and bundles_path.stat().st_size > 10
        except Exception:
            pass

    if parse_status == "FAILED":
        if error_code in ("NEEDS_OCR", "MISSING_SOURCE", "PARSE_ERROR") or "文本层" in error_log:
            steps[0].status = StepStatus.FAILED
            steps[0].error_message = error_log or error_code
        elif (
            bundles_exist
            or "模型" in error_log
            or "API" in error_log
            or "fast_llm" in error_log
            or "LLM" in error_log
            or "网关" in error_log
            or "未配置" in error_log
            or "模型提取失败" in error_log
            or "知识卡" in error_log
            or "候选片段" in error_log
            or "提炼" in error_log
        ):
            # 步骤 1 (提取文字) 与 步骤 2 (信息提取/证据包) 已经成功完成，
            # 失败发生在步骤 3 (AI 快速提取 / L2 卡片提炼)
            steps[0].status = StepStatus.COMPLETED
            steps[1].status = StepStatus.COMPLETED
            steps[2].status = StepStatus.FAILED
            steps[2].error_message = error_log or "AI 快速提取失败"
        else:
            steps[0].status = StepStatus.COMPLETED
            steps[1].status = StepStatus.FAILED
            steps[1].error_message = error_log or "信息提取失败"
    elif parse_status == "LIGHT_PARSED":
        steps[0].status = StepStatus.COMPLETED
        steps[1].status = StepStatus.COMPLETED
        steps[2].status = StepStatus.PENDING
        steps[3].status = StepStatus.PENDING
        steps[4].status = StepStatus.PENDING
    elif parse_status == "LLM_EXTRACTED":
        steps[0].status = StepStatus.COMPLETED
        steps[1].status = StepStatus.COMPLETED
        steps[2].status = StepStatus.COMPLETED
        # Step 4 is complete if cards were extracted and written to DB
        if card_count > 0:
            steps[3].status = StepStatus.COMPLETED
            glow = GlowState.GREEN
        else:
            steps[3].status = StepStatus.PENDING

        if has_master:
            steps[4].status = StepStatus.COMPLETED
            glow = GlowState.GOLD
        else:
            steps[4].status = StepStatus.PENDING
    else:  # PENDING or other
        if download_status == "DOWNLOADED" or doc.get("local_pdf_path"):
            steps[0].status = StepStatus.PENDING
        elif download_status in ("DOWNLOADING", "QUEUED"):
            steps[0].status = StepStatus.PENDING
        elif download_status == "FAILED":
            steps[0].status = StepStatus.FAILED
            steps[0].error_message = "文献下载失败"

    # Identify current active/next step
    current_step = 1
    for step in steps:
        if step.status == StepStatus.COMPLETED:
            current_step = min(5, step.index + 1)
        elif step.status in (StepStatus.ACTIVE, StepStatus.FAILED):
            current_step = step.index
            break
        else:
            current_step = step.index
            break

    return DocumentPipelineStatus(
        doc_id=doc_id,
        steps=steps,
        glow=glow,
        card_count=card_count,
        has_master=has_master,
        current_step=current_step,
    )


def rollback_pipeline_from_step(
    db: Any,
    workspace: Path | str,
    doc_id: str,
    target_step: int,
    *,
    qdrant: Any = None,
) -> dict[str, Any]:
    """Cascading rollback starting from target_step.

    - Target Step 1 (TXT_EXTRACT):
      Invalidates downstream steps 2, 3, 4, 5.
      Deletes all unverified cards for doc_id, detaches/deletes master cards,
      sets parse_status to PENDING, extinguishes all glow.

    - Target Step 2 (INFO_EXTRACT):
      Preserves parsed text.
      Invalidates downstream steps 3, 4, 5.
      Deletes unverified cards and master cards, sets parse_status to PENDING,
      extinguishes all glow.

    - Target Step 3 (AI_FAST):
      Preserves parsed text and bundles.
      Invalidates downstream steps 4, 5.
      Deletes unverified cards and master cards, sets parse_status to LIGHT_PARSED,
      extinguishes all glow.

    - Target Step 4 (DB_WRITE):
      Used to re-commit or re-validate staging cards.

    - Target Step 5 (AI_DEEP):
      Preserves L2 cards (Step 4 DB_WRITE intact, keeping GREEN glow as baseline).
      Only deletes existing L1 Master cards for this doc so it can be re-synthesized.
      Glow steps down from GOLD to GREEN.
    """
    ws = Path(workspace)
    deleted_cards: list[str] = []
    deleted_masters: list[str] = []

    cards = db.cards_for_document(doc_id)

    with getattr(db, "transaction", lambda: _DummyCtx())():
        if target_step in (1, 2, 3):
            # Roll back all unverified cards
            for card in cards:
                knw_id = card.get("knw_id")
                if not knw_id:
                    continue
                if card.get("is_user_verified"):
                    continue
                level = str(card.get("card_level") or "L2").upper()
                c_type = str(card.get("card_type") or "").upper()
                if level == "L1" or c_type == "MASTER":
                    db.delete_knowledge_card(knw_id)
                    deleted_masters.append(knw_id)
                else:
                    db.delete_knowledge_card(knw_id)
                    deleted_cards.append(knw_id)

            if target_step in (1, 2):
                db.update_parse_status(doc_id, "PENDING")
            elif target_step == 3:
                db.update_parse_status(doc_id, "LIGHT_PARSED")

        elif target_step == 5:
            # Only remove existing master cards for this doc
            for card in cards:
                knw_id = card.get("knw_id")
                if not knw_id:
                    continue
                level = str(card.get("card_level") or "L2").upper()
                c_type = str(card.get("card_type") or "").upper()
                if level == "L1" or c_type == "MASTER":
                    db.delete_knowledge_card(knw_id)
                    deleted_masters.append(knw_id)

        db.log_event(
            "INFO",
            "stepper",
            f"{doc_id} 从步骤 {target_step} 触发级联回滚 (删卡: {len(deleted_cards)}张, 删主卡: {len(deleted_masters)}张)",
            doc_id=doc_id,
        )

    # Sync Qdrant vector store if provided
    if qdrant is not None:
        all_to_remove = deleted_cards + deleted_masters
        for cid in all_to_remove:
            try:
                qdrant.delete_card(cid)
            except Exception:
                log.debug("Qdrant delete failed for %s", cid, exc_info=True)

    return {
        "doc_id": doc_id,
        "target_step": target_step,
        "deleted_cards": deleted_cards,
        "deleted_masters": deleted_masters,
    }


class _DummyCtx:
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        return False
