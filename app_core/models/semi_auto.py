"""Persistent web-AI semi-automatic extraction batches.

The GUI uses this module to turn all pending evidence bundles of one or more
documents into a set of copyable prompts.  The prompts are generated locally,
split by a user-selected token budget, and stored under
``workspace/semi_auto/<run_id>``.  Replies are parsed and stored to disk first;
the knowledge base is only modified by an explicit final ``写入知识库`` step.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from models.atomic_prompts import (
    CARD_SYSTEM_PROMPT,
    MANUAL_MODE_INSTRUCTION,
    ExtractionResult,
)
from models.endpoint_pool import EndpointNode
from models.web_ai_bridge import ClipboardBatchService
from parser.batch_packer import DistilledItem, DynamicBatchPacker
from parser.context_bundler import ContextBundle, bundle_hash

SCHEMA = "acdb.semi_auto.v1"
DEFAULT_MAX_BATCH_TOKENS = 80_000
DEFAULT_AGENT_MODEL: str = "GPT-6.2 Sol Ultra"
MANUAL_DOC_TYPES = {"MAN", "BOK"}


class SemiAutoError(RuntimeError):
    pass


class BatchMisalignedError(SemiAutoError):
    def __init__(
        self,
        message: str,
        *,
        target_batch: dict[str, Any],
        current_batch: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.target_batch = target_batch
        self.current_batch = current_batch


def semi_auto_root(workspace: str | Path) -> Path:
    return Path(workspace) / "semi_auto"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_dir(workspace: str | Path, run_id: str) -> Path:
    return semi_auto_root(workspace) / str(run_id)


def load_run(workspace: str | Path, run_id: str) -> dict[str, Any] | None:
    path = _run_dir(workspace, run_id) / "manifest.json"
    if not path.is_file():
        return None
    data = _read_json(path)
    return data if isinstance(data, dict) else None


def save_run(workspace: str | Path, manifest: Mapping[str, Any]) -> None:
    _write_json(
        _run_dir(workspace, str(manifest["run_id"])) / "manifest.json",
        manifest,
    )


def list_runs(workspace: str | Path) -> list[dict[str, Any]]:
    root = semi_auto_root(workspace)
    if not root.is_dir():
        return []
    runs: list[tuple[float, dict[str, Any]]] = []
    for path in root.glob("*/manifest.json"):
        try:
            data = _read_json(path)
            if isinstance(data, dict):
                runs.append((path.stat().st_mtime, data))
        except (OSError, ValueError):
            continue
    runs.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in runs]


def find_unfinished_run(
    workspace: str | Path, doc_ids: Iterable[str] | None = None
) -> dict[str, Any] | None:
    wanted = {str(item).upper() for item in (doc_ids or []) if item}
    unfinished = [
        manifest
        for manifest in list_runs(workspace)
        if str(manifest.get("status") or "").upper() != "FINALIZED"
    ]
    if not unfinished:
        return None

    if wanted:
        # 1. 优先：文献集合完全匹配（精准命中）
        for manifest in unfinished:
            documents = manifest.get("documents") or []
            actual = {
                str(item.get("doc_id") or "").upper()
                for item in documents
                if isinstance(item, Mapping)
            }
            if actual == wanted:
                return manifest

        # 2. 次优：目标文献为未完成任务包文献的子集（如从单篇文献详情页打开，或文库部分选中打开）
        for manifest in unfinished:
            documents = manifest.get("documents") or []
            actual = {
                str(item.get("doc_id") or "").upper()
                for item in documents
                if isinstance(item, Mapping)
            }
            if wanted.issubset(actual):
                return manifest

        # 3. 兜底：目标文献与未完成任务包有交集
        for manifest in unfinished:
            documents = manifest.get("documents") or []
            actual = {
                str(item.get("doc_id") or "").upper()
                for item in documents
                if isinstance(item, Mapping)
            }
            if wanted & actual:
                return manifest

    return unfinished[0] if not wanted else None


def _bundle_path(workspace: Path, document: Mapping[str, Any]) -> Path:
    local = document.get("local_pdf_path")
    if not local:
        raise SemiAutoError(f"{document.get('doc_id')} 没有本地 PDF")
    pdf = Path(str(local))
    if not pdf.is_absolute():
        pdf = workspace / pdf
    path = pdf.parent / "bundles.json"
    if not path.is_file():
        raise SemiAutoError(
            f"{document.get('doc_id')} 尚未生成证据包，请先解析文献"
        )
    return path


def _document_items(
    db: Any,
    workspace: Path,
    document: Mapping[str, Any],
    *,
    force: bool,
) -> list[DistilledItem]:
    doc_id = str(document.get("doc_id") or "")
    raw = _read_json(_bundle_path(workspace, document))
    if not isinstance(raw, list):
        raise SemiAutoError(f"{doc_id} 的 bundles.json 不是数组")
    stored = set() if force else db.bundle_hashes_for_document(doc_id)
    title = str(document.get("title") or "")
    items: list[DistilledItem] = []
    for index, payload in enumerate(raw):
        try:
            bundle = ContextBundle.model_validate(payload)
        except Exception:
            continue
        if not force and bundle_hash(bundle) in stored:
            continue
        item_id = f"{doc_id}-{index}"
        items.append(
            DistilledItem(
                item_id=item_id,
                text=bundle.prompt_markdown(),
                metadata={
                    "doc_id": doc_id,
                    "title": title,
                    "bundle": bundle.model_dump(mode="json"),
                },
            )
        )
    return items


def _premise(documents: list[Mapping[str, Any]], item_count: int) -> str:
    lines = [
        "【本次网页 AI 半自动提炼任务】",
        f"共 {len(documents)} 篇文献、{item_count} 个待处理 ITEM。",
        "每个 ITEM 开始处标注了所属文献；请按 ITEM 分别输出，不要跨 ITEM 合并。",
        "文献清单：",
    ]
    for document in documents:
        lines.append(
            f"- {document.get('doc_id')} · {document.get('title') or ''}"
        )
    return "\n".join(lines)


def create_run(
    db: Any,
    workspace: str | Path,
    documents: Iterable[Mapping[str, Any]],
    pipeline: Any,
    *,
    max_batch_tokens: int = DEFAULT_MAX_BATCH_TOKENS,
    mode: str = "force",
    agent_model: str = "",
) -> dict[str, Any]:
    """Create prompts and a persistent manifest for the selected documents."""
    workspace = Path(workspace)
    docs = [dict(item) for item in documents if item]
    if not docs:
        raise SemiAutoError("没有选择文献")
    force = str(mode).lower() == "force"
    budget = max(2048, int(max_batch_tokens))
    node = EndpointNode(
        node_id="semi-auto-batch",
        name="网页 AI 半自动批量",
        node_type="OPENAI_COMPATIBLE_API",
        capabilities=["TEXT_LLM"],
        max_input_tokens=budget,
        max_output_tokens=min(64_000, max(8_000, budget // 2)),
        max_items_per_batch=500,
    )

    doc_packets: list[tuple[dict[str, Any], Any]] = []
    total_items = 0
    warnings: list[dict[str, str]] = []

    # Constraint: Packets must strictly contain chunks from a single document. Never mix documents.
    for document in docs:
        doc_id = str(document.get("doc_id") or "")
        try:
            doc_items = _document_items(db, workspace, document, force=force)
        except SemiAutoError as exc:
            warnings.append({"doc_id": doc_id, "error": str(exc)})
            continue
        if not doc_items:
            continue
        total_items += len(doc_items)
        doc_manual = str(document.get("doc_type") or "").upper() in MANUAL_DOC_TYPES
        doc_system = CARD_SYSTEM_PROMPT.replace("{doc_id}", doc_id)
        if doc_manual:
            doc_system += "\n\n" + MANUAL_MODE_INSTRUCTION
        doc_premise = _premise([document], len(doc_items))
        packer = DynamicBatchPacker(
            system_prompt=doc_system,
            global_premise=doc_premise,
        )
        service = ClipboardBatchService(packer)
        try:
            pkts = service.build_packets(
                doc_items,
                node,
                system_prompt=doc_system,
                global_premise=doc_premise,
            )
            for pkt in pkts:
                doc_packets.append((document, pkt))
        except Exception as exc:
            raise SemiAutoError(f"生成文献 {doc_id} 提问包失败：{exc}") from exc

    if not doc_packets:
        detail = "；".join(
            f"{item['doc_id']}: {item['error']}" for item in warnings[:3]
        )
        raise SemiAutoError(
            "没有待处理证据包。请先解析/OCR 文献，或改用强制重新提炼。"
            + (f"（{detail}）" if detail else "")
        )

    run_id = "semi_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    root = _run_dir(workspace, run_id)
    root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "WAITING",
        "mode": "force" if force else "incremental",
        "max_batch_tokens": budget,
        "agent_model": str(agent_model or DEFAULT_AGENT_MODEL).strip(),
        "documents": [
            {
                "doc_id": str(document.get("doc_id") or ""),
                "title": str(document.get("title") or ""),
                "doc_type": str(document.get("doc_type") or ""),
                "local_pdf_path": str(document.get("local_pdf_path") or ""),
            }
            for document in docs
        ],
        "item_count": total_items,
        "warnings": warnings,
        "batches": [],
    }
    for slot, (document, packet) in enumerate(doc_packets, start=1):
        batch_id = f"BATCH_{slot:02d}"
        batch_dir = root / f"batch_{slot:02d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = batch_dir / "prompt.txt"
        prompt_text = (
            f"【提问包】{batch_id} · 槽位 {slot:02d}（共 {len(packet.batch.items)} 个待提炼证据片段）\n\n"
            + packet.prompt
        )
        prompt_path.write_text(prompt_text, encoding="utf-8")
        batch_items = []
        for item in packet.batch.items:
            metadata = item.metadata or {}
            batch_items.append(
                {
                    "item_id": item.item_id,
                    "doc_id": str(metadata.get("doc_id") or ""),
                    "bundle": metadata.get("bundle") or {},
                }
            )
        items_path = batch_dir / "items.json"
        _write_json(items_path, batch_items)
        manifest["batches"].append(
            {
                "batch_id": batch_id,
                "slot": slot,
                "item_ids": [item.item_id for item in packet.batch.items],
                "doc_ids": [str(document.get("doc_id") or "")],
                "doc_title": str(document.get("title") or ""),
                "album_pdf_path": str(document.get("local_pdf_path") or ""),
                "estimated_tokens": int(packet.batch.input_tokens),
                "status": "PENDING",
                "prompt_path": f"batch_{slot:02d}/prompt.txt",
                "items_path": f"batch_{slot:02d}/items.json",
                "reply_path": "",
                "imported_item_ids": [],
            }
        )
    save_run(workspace, manifest)
    return manifest


def regenerate_batches(
    db: Any,
    workspace: str | Path,
    manifest: dict[str, Any],
    *,
    max_batch_tokens: int = DEFAULT_MAX_BATCH_TOKENS,
    pipeline: Any = None,
) -> dict[str, Any]:
    """Regenerate pending batches while preserving all imported batches and their replies.

    - Batches with status == 'IMPORTED' (or having a reply_path) are kept intact.
    - Pending/unanswered batches are removed.
    - Any items not yet covered by kept imported batches are re-partitioned
      into new batches according to `max_batch_tokens`.
    - Batches are re-slotted sequentially (1, 2, ..., N) in document and item order.
    """
    workspace = Path(workspace)
    run_id = str(manifest.get("run_id") or "")
    if not run_id:
        raise SemiAutoError("任务包不存在 run_id")
    root = _run_dir(workspace, run_id)
    if not root.is_dir():
        raise SemiAutoError(f"任务包目录不存在：{root}")

    force = str(manifest.get("mode", "force")).lower() == "force"
    budget = max(2048, int(max_batch_tokens))
    node = EndpointNode(
        node_id="semi-auto-batch",
        name="网页 AI 半自动批量",
        node_type="OPENAI_COMPATIBLE_API",
        capabilities=["TEXT_LLM"],
        max_input_tokens=budget,
        max_output_tokens=min(64_000, max(8_000, budget // 2)),
        max_items_per_batch=500,
    )

    batches = list(manifest.get("batches") or [])
    imported_batches: list[dict[str, Any]] = []
    pending_batches: list[dict[str, Any]] = []

    for b in batches:
        has_reply = bool(b.get("reply_path")) or str(b.get("status") or "").upper() == "IMPORTED"
        if has_reply:
            imported_batches.append(dict(b))
        else:
            pending_batches.append(dict(b))

    covered_item_ids: set[str] = set()
    for b in imported_batches:
        ids = b.get("item_ids") or b.get("imported_item_ids") or []
        covered_item_ids.update(str(x) for x in ids)

    docs = list(manifest.get("documents") or [])
    new_doc_packets: list[tuple[dict[str, Any], Any]] = []

    for document in docs:
        doc_id = str(document.get("doc_id") or "")
        full_doc = db.get_document(doc_id) if db else None
        doc_info = dict(full_doc) if full_doc else dict(document)
        if not doc_info.get("local_pdf_path") and document.get("local_pdf_path"):
            doc_info["local_pdf_path"] = document.get("local_pdf_path")
        try:
            doc_items = _document_items(db, workspace, doc_info, force=force)
        except SemiAutoError:
            continue
        if not doc_items:
            continue

        remaining_items = [
            it for it in doc_items if str(it.item_id) not in covered_item_ids
        ]
        if not remaining_items:
            continue

        doc_manual = str(doc_info.get("doc_type") or "").upper() in MANUAL_DOC_TYPES
        doc_system = CARD_SYSTEM_PROMPT.replace("{doc_id}", doc_id)
        if doc_manual:
            doc_system += "\n\n" + MANUAL_MODE_INSTRUCTION
        doc_premise = _premise([doc_info], len(remaining_items))
        packer = DynamicBatchPacker(
            system_prompt=doc_system,
            global_premise=doc_premise,
        )
        service = ClipboardBatchService(packer)
        try:
            pkts = service.build_packets(
                remaining_items,
                node,
                system_prompt=doc_system,
                global_premise=doc_premise,
            )
            for pkt in pkts:
                new_doc_packets.append((doc_info, pkt))
        except Exception as exc:
            raise SemiAutoError(f"生成文献 {doc_id} 提问包失败：{exc}") from exc

    if not new_doc_packets and not pending_batches:
        manifest["max_batch_tokens"] = budget
        save_run(workspace, manifest)
        return manifest

    # Clean up disk
    for b in pending_batches:
        slot = int(b.get("slot") or 0)
        p_dir = root / f"batch_{slot:02d}"
        shutil.rmtree(p_dir, ignore_errors=True)

    temp_stage = root / "_temp_imported_stage"
    temp_stage.mkdir(parents=True, exist_ok=True)
    for idx, b in enumerate(imported_batches):
        old_slot = int(b.get("slot") or 0)
        old_dir = root / f"batch_{old_slot:02d}"
        stage_dir = temp_stage / f"imported_{idx:03d}"
        if old_dir.is_dir():
            shutil.move(str(old_dir), str(stage_dir))
        b["_stage_dir"] = stage_dir

    def _item_seq(item_id: str) -> int:
        try:
            return int(str(item_id).rsplit("-", 1)[-1])
        except Exception:
            return 0

    all_ordered_entries: list[tuple[str, Any]] = []

    for document in docs:
        doc_id = str(document.get("doc_id") or "")
        doc_entries: list[tuple[int, str, Any]] = []

        for b in imported_batches:
            b_docs = [str(x) for x in b.get("doc_ids") or []]
            if doc_id in b_docs:
                first_item = (b.get("item_ids") or ["0"])[0]
                seq = _item_seq(first_item)
                doc_entries.append((seq, "imported", b))

        for doc_info, pkt in new_doc_packets:
            p_doc_id = str(doc_info.get("doc_id") or "")
            if p_doc_id == doc_id:
                first_item = pkt.batch.items[0].item_id if pkt.batch.items else "0"
                seq = _item_seq(first_item)
                doc_entries.append((seq, "new", (doc_info, pkt)))

        doc_entries.sort(key=lambda x: x[0])
        for _, kind, val in doc_entries:
            all_ordered_entries.append((kind, val))

    doc_ids_set = {str(d.get("doc_id") or "") for d in docs}
    for b in imported_batches:
        b_docs = {str(x) for x in b.get("doc_ids") or []}
        if not (b_docs & doc_ids_set):
            all_ordered_entries.append(("imported", b))

    final_batches: list[dict[str, Any]] = []
    for slot, (kind, val) in enumerate(all_ordered_entries, start=1):
        batch_id = f"BATCH_{slot:02d}"
        batch_dir = root / f"batch_{slot:02d}"
        if kind == "imported":
            b = val
            stage_dir = b.pop("_stage_dir", None)
            if stage_dir and stage_dir.is_dir():
                shutil.move(str(stage_dir), str(batch_dir))
            old_slot = int(b.get("slot") or 0)
            b["slot"] = slot
            b["batch_id"] = batch_id
            b["prompt_path"] = f"batch_{slot:02d}/prompt.txt"
            b["items_path"] = f"batch_{slot:02d}/items.json"
            if b.get("reply_path"):
                b["reply_path"] = f"batch_{slot:02d}/reply.txt"

            prompt_file = batch_dir / "prompt.txt"
            if prompt_file.is_file() and old_slot != slot:
                try:
                    txt = prompt_file.read_text(encoding="utf-8")
                    import re
                    new_txt = re.sub(
                        r"^【提问包】BATCH_\d+ · 槽位 \d+",
                        f"【提问包】{batch_id} · 槽位 {slot:02d}",
                        txt,
                        count=1,
                    )
                    prompt_file.write_text(new_txt, encoding="utf-8")
                except Exception:
                    pass

            final_batches.append(b)
        else:
            doc_info, packet = val
            batch_dir.mkdir(parents=True, exist_ok=True)
            prompt_path = batch_dir / "prompt.txt"
            prompt_text = (
                f"【提问包】{batch_id} · 槽位 {slot:02d}（共 {len(packet.batch.items)} 个待提炼证据片段）\n\n"
                + packet.prompt
            )
            prompt_path.write_text(prompt_text, encoding="utf-8")
            batch_items = []
            for item in packet.batch.items:
                metadata = item.metadata or {}
                batch_items.append(
                    {
                        "item_id": item.item_id,
                        "doc_id": str(metadata.get("doc_id") or ""),
                        "bundle": metadata.get("bundle") or {},
                    }
                )
            items_path = batch_dir / "items.json"
            _write_json(items_path, batch_items)
            final_batches.append(
                {
                    "batch_id": batch_id,
                    "slot": slot,
                    "item_ids": [item.item_id for item in packet.batch.items],
                    "doc_ids": [str(doc_info.get("doc_id") or "")],
                    "doc_title": str(doc_info.get("title") or ""),
                    "album_pdf_path": str(doc_info.get("local_pdf_path") or ""),
                    "estimated_tokens": int(packet.batch.input_tokens),
                    "status": "PENDING",
                    "prompt_path": f"batch_{slot:02d}/prompt.txt",
                    "items_path": f"batch_{slot:02d}/items.json",
                    "reply_path": "",
                    "imported_item_ids": [],
                }
            )

    shutil.rmtree(temp_stage, ignore_errors=True)

    manifest["batches"] = final_batches
    manifest["max_batch_tokens"] = budget
    manifest["status"] = (
        "READY"
        if final_batches and all(str(b.get("status")).upper() == "IMPORTED" for b in final_batches)
        else "WAITING"
    )
    save_run(workspace, manifest)
    return manifest


def read_prompt(workspace: str | Path, manifest: Mapping[str, Any], batch: Mapping[str, Any]) -> str:
    path = _run_dir(workspace, str(manifest["run_id"])) / str(batch["prompt_path"])
    if not path.is_file():
        raise SemiAutoError("提示词文件不存在，请重新生成提问包")
    text = path.read_text(encoding="utf-8")
    batch_id = str(batch.get("batch_id") or "")
    slot = int(batch.get("slot") or 1)
    if batch_id and not text.startswith("【提问包】"):
        item_ids = batch.get("item_ids") or []
        count_str = f"（共 {len(item_ids)} 个待提炼证据片段）\n\n" if item_ids else "\n\n"
        text = f"【提问包】{batch_id} · 槽位 {slot:02d}{count_str}{text}"
    return text


def read_items(
    workspace: str | Path, manifest: Mapping[str, Any], batch: Mapping[str, Any]
) -> list[dict[str, Any]]:
    path = _run_dir(workspace, str(manifest["run_id"])) / str(batch["items_path"])
    data = _read_json(path)
    return data if isinstance(data, list) else []


def import_reply(
    workspace: str | Path,
    manifest: dict[str, Any],
    batch: dict[str, Any],
    text: str,
    *,
    source_label: str = "",
    agent_model: str = "",
) -> dict[str, Any]:
    """Parse and persist a reply without touching the knowledge database."""
    service = ClipboardBatchService()
    try:
        results = service.parse_reply(text)
    except Exception as exc:
        raise SemiAutoError(f"回复格式错误：{exc}") from exc
    items = read_items(workspace, manifest, batch)
    expected = {str(item.get("item_id") or "") for item in items}
    received = set(results) & expected
    if not received:
        # Check if the pasted reply actually belongs to another batch in the manifest
        other_matches = []
        for other_b in manifest.get("batches") or []:
            if str(other_b.get("batch_id")) == str(batch.get("batch_id")):
                continue
            other_expected = set(other_b.get("item_ids") or [])
            if set(results) & other_expected:
                other_matches.append(other_b)
        if other_matches:
            target_b = other_matches[0]
            slot_curr = int(batch.get("slot") or 0)
            slot_tgt = int(target_b.get("slot") or 0)
            raise BatchMisalignedError(
                f"【提问包错位拦截】\n"
                f"您当前选中的是【提问包 {slot_curr:02d} ({batch.get('batch_id')})】，\n"
                f"但粘贴的回复实际属于【提问包 {slot_tgt:02d} ({target_b.get('batch_id')})】！\n\n"
                f"系统已为您自动拦截以防数据覆盖。是否直接将此回复导入到【提问包 {slot_tgt:02d}】？",
                target_batch=target_b,
                current_batch=batch,
            )
        sample_exp = ", ".join(sorted(expected)[:3])
        valid_rec = [k for k in sorted(results) if k.strip()]
        sample_rec = ", ".join(valid_rec[:3]) if valid_rec else "（未识别出有效片段 ID）"
        raise SemiAutoError(
            f"【提问包不匹配】\n"
            f"回复中包含的片段（{sample_rec} 等）与当前提问包要求的片段（{sample_exp} 等）不符，请确认是否粘贴错了其他文献的提问包回复。"
        )
    missing = sorted(expected - received)
    card_count = sum(len(result.cards) for result in results.values())
    root = _run_dir(workspace, str(manifest["run_id"]))
    reply_path = root / f"batch_{int(batch['slot']):02d}" / "reply.txt"
    reply_path.write_text(str(text), encoding="utf-8")
    batch["reply_path"] = f"batch_{int(batch['slot']):02d}/reply.txt"
    batch["imported_item_ids"] = sorted(received)
    batch["status"] = "IMPORTED" if not missing else "PARTIAL"
    batch["imported_at"] = datetime.now().isoformat(timespec="seconds")
    if source_label:
        batch["imported_by"] = str(source_label).strip()
    if agent_model:
        batch["agent_model"] = str(agent_model).strip()
    manifest["status"] = (
        "READY"
        if all(
            str(item.get("status") or "").upper() == "IMPORTED"
            for item in manifest.get("batches") or []
        )
        else "WAITING"
    )
    save_run(workspace, manifest)
    return {
        "batch_id": batch["batch_id"],
        "received": len(received),
        "expected": len(expected),
        "missing": missing,
        "cards": card_count,
        "status": batch["status"],
    }


def _load_parsed_replies(
    workspace: str | Path,
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    service = ClipboardBatchService()
    parsed: dict[str, dict[str, Any]] = {}
    for batch in manifest.get("batches") or []:
        if str(batch.get("status") or "").upper() != "IMPORTED":
            raise SemiAutoError(
                f"{batch.get('batch_id')} 尚未完整导入，不能写入知识库"
            )
        reply_path = str(batch.get("reply_path") or "")
        if not reply_path:
            raise SemiAutoError(f"{batch.get('batch_id')} 缺少回复文件")
        path = _run_dir(workspace, str(manifest["run_id"])) / reply_path
        try:
            results = service.parse_reply(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SemiAutoError(
                f"{batch.get('batch_id')} 回复解析失败：{exc}"
            ) from exc
        items = read_items(workspace, manifest, batch)
        expected = {str(item.get("item_id") or "") for item in items}
        missing = expected - set(results)
        if missing:
            raise SemiAutoError(
                f"{batch.get('batch_id')} 缺少 {len(missing)} 个 item 的回复"
            )
        parsed[str(batch["batch_id"])] = {
            "results": results,
            "items": items,
        }
    return parsed


def _batch_provenance(
    batch: Mapping[str, Any],
    *,
    default_agent_name: str = "",
    default_agent_model: str = "",
) -> dict[str, dict[str, Any]]:
    imported_by = str(batch.get("imported_by") or default_agent_name or "").strip()
    agent_model = str(batch.get("agent_model") or default_agent_model or "").strip()
    if imported_by and imported_by not in ("网页版 AI 手动", "网页半自动任务台", "外部网页 AI"):
        return {
            "fast_llm": {
                "node_id": f"mcp-{imported_by.lower()}",
                "name": imported_by,
                "node_type": "MCP_AGENT",
                "provider": "mcp",
                "model": agent_model or imported_by,
            }
        }
    return {
        "fast_llm": {
            "node_id": "semi-auto",
            "name": imported_by or "网页版 AI 手动",
            "node_type": "SEMI_AUTO_CLIPBOARD",
            "provider": "semi_auto",
            "model": agent_model or DEFAULT_AGENT_MODEL,
        }
    }


def finalize_run(
    pipeline: Any,
    db: Any,
    workspace: str | Path,
    manifest: dict[str, Any],
    *,
    agent_name: str = "",
    agent_model: str = "",
) -> dict[str, Any]:
    """Delete old unverified cards (force mode), then write all imported replies atomically."""
    if str(manifest.get("status") or "").upper() == "FINALIZED":
        return {"stored": 0, "documents": 0, "already_finalized": True}
    parsed = _load_parsed_replies(workspace, manifest)
    doc_ids = [
        str(item.get("doc_id") or "")
        for item in manifest.get("documents") or []
    ]
    stored = 0
    skipped_items = 0
    empty_items = 0
    with_cards: set[str] = set()
    deleted_qdrant_ids: list[str] = []
    verified_events: list[tuple[str, int]] = []
    tx_factory = getattr(db, "transaction", None)
    tx_ctx = tx_factory() if callable(tx_factory) else contextlib.nullcontext()
    with tx_ctx:
        if str(manifest.get("mode") or "").lower() == "force":
            for doc_id in doc_ids:
                if not doc_id:
                    continue
                removed = db.delete_unverified_cards_for_document(doc_id)
                deleted_qdrant_ids.extend(
                    str(item) for item in (removed.get("deleted") or []) if item
                )
                if removed.get("kept_verified"):
                    verified_events.append((doc_id, len(removed["kept_verified"])))
        for batch in manifest.get("batches") or []:
            payload = parsed.get(str(batch["batch_id"]))
            if not payload:
                continue
            provenance_override = _batch_provenance(
                batch,
                default_agent_name=agent_name,
                default_agent_model=agent_model,
            )
            results = payload["results"]
            for item in payload["items"]:
                item_id = str(item.get("item_id") or "")
                result = results.get(item_id)
                if result is None:
                    continue
                if result.skip:
                    skipped_items += 1
                    continue
                if not result.cards:
                    empty_items += 1
                    continue
                bundle_payload = item.get("bundle") or {}
                try:
                    bundle = ContextBundle.model_validate(bundle_payload)
                except Exception:
                    continue
                document = db.get_document(str(item.get("doc_id") or ""))
                if not document:
                    continue
                effective_model = (
                    str(batch.get("agent_model") or agent_model or DEFAULT_AGENT_MODEL).strip()
                )
                for draft in result.cards:
                    if hasattr(draft, "structured_details"):
                        struct = dict(draft.structured_details or {})
                        struct["extractor_model"] = effective_model
                        draft.structured_details = struct
                    try:
                        stored_id = pipeline._store_draft(
                            draft,
                            document,
                            bundle,
                            result.keywords,
                            provenance_override=provenance_override,
                            provenance_source="semi_auto",
                        )
                    except TypeError:
                        stored_id = pipeline._store_draft(
                            draft, document, bundle, result.keywords
                        )
                    if stored_id:
                        stored += 1
                        with_cards.add(str(document["doc_id"]))
        for doc_id in with_cards:
            try:
                db.update_parse_status(doc_id, "LLM_EXTRACTED")
            except Exception:
                pass
    qdrant = getattr(pipeline, "qdrant", None)
    if qdrant is not None:
        for knw_id in deleted_qdrant_ids:
            try:
                qdrant.delete(knw_id)
            except Exception:
                pass
    for doc_id, kept_count in verified_events:
        try:
            db.log_event(
                "INFO",
                "semi_auto",
                f"{doc_id} 半自动强制重提保留人工确认卡片 {kept_count} 张",
                doc_id=doc_id,
            )
        except Exception:
            pass
    manifest["status"] = "FINALIZED"
    manifest["finalized_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["stored_cards"] = int(stored)
    save_run(workspace, manifest)
    try:
        db.log_event(
            "INFO",
            "semi_auto",
            f"半自动/Agent 批量写入：文献 {len(with_cards)} 篇，新卡 {stored} 张",
        )
    except Exception:
        pass
    return {
        "stored": stored,
        "documents": len(with_cards),
        "skipped_items": skipped_items,
        "empty_items": empty_items,
        "already_finalized": False,
    }



def parse_documents(
    pipeline: Any,
    doc_ids: Iterable[str],
    *,
    progress: Any = None,
) -> dict[str, Any]:
    """Run parse/OCR only, without extraction, for the batch workspace."""
    results = {"parsed": 0, "failed": [], "bundles": 0, "parsed_doc_ids": []}
    ids = [str(item) for item in doc_ids if item]
    for index, doc_id in enumerate(ids, start=1):
        if progress:
            progress(f"解析/OCR {index}/{len(ids)} · {doc_id}", 0.0)
        try:
            parsed = pipeline.parse_document(str(doc_id))
        except Exception as exc:
            results["failed"].append({"doc_id": str(doc_id), "error": str(exc)})
            continue
        if parsed is None:
            results["failed"].append(
                {"doc_id": str(doc_id), "error": "解析失败或需要 OCR"}
            )
            continue
        _document, bundles, _path = parsed
        results["parsed"] += 1
        results["bundles"] += len(bundles)
        results["parsed_doc_ids"].append(str(doc_id))
        if progress:
            progress(
                f"解析/OCR {index}/{len(ids)} · {doc_id} · {len(bundles)} 个证据包",
                index / max(1, len(ids)),
            )
    return results


def delete_batch(
    workspace: str | Path, manifest: dict[str, Any], batch_id: str
) -> dict[str, Any] | None:
    root = _run_dir(workspace, str(manifest["run_id"]))
    batches = list(manifest.get("batches") or [])
    kept = []
    for batch in batches:
        if str(batch.get("batch_id")) == str(batch_id):
            slot = int(batch.get("slot") or 0)
            shutil.rmtree(root / f"batch_{slot:02d}", ignore_errors=True)
            continue
        kept.append(batch)
    manifest["batches"] = kept
    if not kept:
        shutil.rmtree(root, ignore_errors=True)
        return None
    save_run(workspace, manifest)
    return manifest


def delete_run(workspace: str | Path, run_id: str) -> None:
    shutil.rmtree(_run_dir(workspace, run_id), ignore_errors=True)


__all__ = [
    "DEFAULT_AGENT_MODEL",
    "DEFAULT_MAX_BATCH_TOKENS",
    "SemiAutoError",
    "create_run",
    "delete_batch",
    "delete_run",
    "finalize_run",
    "find_unfinished_run",
    "import_reply",
    "list_runs",
    "parse_documents",
    "load_run",
    "read_items",
    "read_prompt",
    "regenerate_batches",
    "save_run",
    "semi_auto_root",
]
