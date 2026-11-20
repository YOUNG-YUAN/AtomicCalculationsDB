"""Agent Task Packager for AtomicCalculationsDB.

Prepares self-explaining, file-based task bundles for autonomous AI agents (Codex,
Claude Code, Antigravity, etc.) and provides validation and automated ingestion.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from models.atomic_prompts import ExtractionResult
from parser.context_bundler import ContextBundle
from parser.table_compressor import skeletonize_table_markdown

log = logging.getLogger(__name__)

_TABLE_REF_RE = re.compile(r"\b(?:Table|TABLE|Tab\.|表)\s*([0-9IVXLC]+)", re.I)


class AgentTaskPackager:
    """Manages creation and ingestion of file-based Agent Task Bundles."""

    def __init__(self, db: Any, workspace: str | Path, *, pipeline: Any = None):
        self.db = db
        self.workspace = Path(workspace)
        self.pipeline = pipeline

    def export_task_bundle(
        self,
        doc_id: str,
        *,
        out_dir: str | Path | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Generate a complete, self-explaining agent task bundle directory for a document."""
        doc_key = str(doc_id or "").strip()
        doc = self.db.get_document(doc_key)
        if not doc:
            raise ValueError(f"找不到文献：{doc_key}")

        local_pdf = doc.get("local_pdf_path")
        if not local_pdf:
            raise ValueError(f"文献 {doc_key} 缺少本地 PDF 文件路径")

        pdf_path = Path(local_pdf)
        if not pdf_path.is_absolute():
            pdf_path = self.workspace / pdf_path
        storage_dir = pdf_path.parent

        bundles_path = storage_dir / "bundles.json"
        parsed_md_path = storage_dir / "parsed.md"

        # If document not parsed yet or forced, run parse_document
        if force or not bundles_path.is_file() or not parsed_md_path.is_file():
            if self.pipeline is not None:
                log.info("Document %s requires parsing before agent task export", doc_key)
                self.pipeline.parse_document(doc_key)
                doc = self.db.get_document(doc_key) or doc
            else:
                raise ValueError(f"文献 {doc_key} 尚未解析生成证据包，且 Pipeline 未提供")

        if not bundles_path.is_file():
            raise ValueError(f"文献 {doc_key} 的 bundles.json 不存在，请先解析文献")

        task_dir = Path(out_dir) if out_dir else (self.workspace / "agent_tasks" / doc_key)
        task_dir.mkdir(parents=True, exist_ok=True)

        # 1. Tables directory: export skeletonized tables
        tables_dir = task_dir / "tables"
        tables_dir.mkdir(exist_ok=True)
        table_files_map: dict[str, str] = {}
        tables_catalog: list[dict[str, str]] = []

        parsed_tables_path = storage_dir / "parsed_tables.json"
        raw_tables = []
        if parsed_tables_path.is_file():
            try:
                raw_tables = json.loads(parsed_tables_path.read_text(encoding="utf-8"))
            except Exception:
                raw_tables = []

        def _clean_caption(raw_cap: Any) -> str:
            if isinstance(raw_cap, Mapping):
                return str(raw_cap.get("content") or raw_cap.get("text") or "").strip()
            s = str(raw_cap or "").strip()
            if s.startswith("{") and ("'content':" in s or '"content":' in s):
                m_c = re.search(r"['\"]content['\"]\s*:\s*['\"]([^'\"]+)['\"]", s)
                if m_c:
                    return m_c.group(1).strip()
            return s

        seen_table_keys: set[str] = set()
        for idx, t in enumerate(raw_tables, start=1):
            caption = _clean_caption(t.get("caption"))
            markdown = str(t.get("markdown") or "").strip()
            if not markdown:
                continue
            m = _TABLE_REF_RE.search(caption)
            if m:
                tnum = m.group(1).upper()
                file_name = f"Table_{tnum}.md"
                table_key = tnum
            else:
                file_name = f"Table_{idx}.md"
                table_key = str(idx)

            table_path = tables_dir / file_name
            is_continuation = "continued" in caption.lower()
            if table_key in seen_table_keys and is_continuation and table_path.is_file():
                continue

            skeleton = skeletonize_table_markdown(markdown, caption=caption)
            table_path.write_text(skeleton, encoding="utf-8")
            rel_file = f"tables/{file_name}"
            table_files_map[table_key] = rel_file
            if table_key not in seen_table_keys:
                seen_table_keys.add(table_key)
                tables_catalog.append({
                    "table_key": table_key,
                    "file": rel_file,
                    "caption": caption or f"Table {table_key}",
                })

        # 2. Images directory: export cropped figures if present
        images_dir = task_dir / "images"
        storage_images = storage_dir / "images"
        copied_images = []
        if storage_images.is_dir():
            images_dir.mkdir(exist_ok=True)
            for img_file in storage_images.glob("*.*"):
                if img_file.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    dest = images_dir / img_file.name
                    try:
                        shutil.copy2(img_file, dest)
                        copied_images.append(f"images/{img_file.name}")
                    except Exception:
                        pass

        # 3. parsed.md: copy cleaned paper Markdown
        if parsed_md_path.is_file():
            shutil.copy2(parsed_md_path, task_dir / "parsed.md")

        # 4. items.json: load bundles and build structured item entries
        bundles_raw = json.loads(bundles_path.read_text(encoding="utf-8"))
        items = []
        for idx, b_data in enumerate(bundles_raw):
            try:
                bundle = ContextBundle.model_validate(b_data)
            except Exception:
                continue

            item_id = f"{doc_key}-{idx}"
            text_to_search = f"{bundle.focal_sentence}\n{bundle.focal_window_markdown}"
            refs = _TABLE_REF_RE.findall(text_to_search)
            matched_refs = []
            matched_files = []
            for r in refs:
                num = r.upper()
                if num in table_files_map and num not in matched_refs:
                    matched_refs.append(num)
                    matched_files.append(table_files_map[num])

            item_images = []
            loc = bundle.locator or {}
            fig_img = loc.get("figure_image") or loc.get("block_figure_image")
            if fig_img:
                img_name = Path(str(fig_img)).name
                if (images_dir / img_name).is_file():
                    item_images.append(f"images/{img_name}")

            items.append({
                "item_id": item_id,
                "section_path": bundle.section_path,
                "page_num": bundle.page_num or 1,
                "focal_sentence": bundle.focal_sentence,
                "focal_window": bundle.focal_window_markdown,
                "backtraced_definitions": bundle.backtraced_definitions,
                "channels": bundle.detected_channels,
                "table_refs": matched_refs,
                "table_files": matched_files,
                "image_files": item_images,
                "bundle_payload": bundle.model_dump(mode="json"),
            })

        items_file = task_dir / "items.json"
        items_file.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

        # 5. schema.json: export strict ExtractionResult JSON Schema
        extraction_schema = ExtractionResult.model_json_schema()
        submission_schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "AtomicPhysicsExtractionSubmission",
            "description": "Output schema for AtomicCalculationsDB autonomous agent extraction",
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "description": "List of extraction outputs for each item in items.json",
                    "items": {
                        "type": "object",
                        "required": ["item_id", "result"],
                        "properties": {
                            "item_id": {
                                "type": "string",
                                "description": "The item_id exactly matching items.json (e.g. DOC-PAP-XXXX-0)",
                            },
                            "result": extraction_schema,
                        },
                    },
                }
            },
        }
        schema_file = task_dir / "schema.json"
        schema_file.write_text(json.dumps(submission_schema, ensure_ascii=False, indent=2), encoding="utf-8")

        # 6. task_instructions.md: write self-explaining instructions
        instructions_text = self._build_instructions_markdown(
            doc=doc,
            doc_key=doc_key,
            item_count=len(items),
            tables_catalog=tables_catalog,
            has_images=bool(copied_images),
            task_dir=task_dir,
        )
        instructions_file = task_dir / "task_instructions.md"
        instructions_file.write_text(instructions_text, encoding="utf-8")

        return {
            "ok": True,
            "doc_id": doc_key,
            "title": str(doc.get("title") or ""),
            "task_dir": str(task_dir.resolve()),
            "instructions_file": str(instructions_file.resolve()),
            "items_file": str(items_file.resolve()),
            "schema_file": str(schema_file.resolve()),
            "item_count": len(items),
            "table_count": len(tables_catalog),
            "image_count": len(copied_images),
        }

    def submit_task_result(
        self,
        doc_id: str,
        result_path_or_json: str | Path | Mapping[str, Any],
        *,
        agent_name: str = "agent",
        agent_model: str = "",
    ) -> dict[str, Any]:
        """Validate and ingest an agent-generated result.json into the knowledge database."""
        doc_key = str(doc_id or "").strip()
        doc = self.db.get_document(doc_key)
        if not doc:
            raise ValueError(f"找不到文献：{doc_key}")

        if self.pipeline is None:
            raise RuntimeError("Pipeline 未初始化，无法写入知识库")

        # Load raw JSON
        if isinstance(result_path_or_json, Mapping):
            payload = dict(result_path_or_json)
        else:
            path_or_text = str(result_path_or_json).strip()
            file_candidate = Path(path_or_text)
            if file_candidate.is_file():
                raw_text = file_candidate.read_text(encoding="utf-8")
            else:
                raw_text = path_or_text
            payload = json.loads(raw_text)

        # Normalize entries
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict) and "items" in payload:
            entries = payload["items"]
        elif isinstance(payload, dict) and "cards" in payload:
            entries = [{"item_id": f"{doc_key}-0", "result": payload}]
        else:
            raise ValueError("回复 JSON 缺少根键 'items' 数组")

        # Load bundles for this document
        task_items_file = self.workspace / "agent_tasks" / doc_key / "items.json"
        bundle_map: dict[str, ContextBundle] = {}
        if task_items_file.is_file():
            try:
                task_items_raw = json.loads(task_items_file.read_text(encoding="utf-8"))
                for t_item in task_items_raw:
                    iid = str(t_item.get("item_id") or "")
                    bp = t_item.get("bundle_payload")
                    if iid and bp:
                        bundle_map[iid] = ContextBundle.model_validate(bp)
            except Exception:
                pass

        if not bundle_map:
            local_pdf = doc.get("local_pdf_path")
            if local_pdf:
                b_path = (self.workspace / local_pdf).parent / "bundles.json"
                if b_path.is_file():
                    for idx, b_item in enumerate(json.loads(b_path.read_text(encoding="utf-8"))):
                        bundle_map[f"{doc_key}-{idx}"] = ContextBundle.model_validate(b_item)

        provenance_override = {
            "fast_llm": {
                "node_id": f"agent-{agent_name.lower()}",
                "name": agent_name or "CLI_AGENT",
                "node_type": "CLI_AGENT",
                "provider": "agent_cli",
                "model": agent_model or "Codex/AutonomousAgent",
            }
        }

        stored_cards = 0
        stored_keywords = 0
        skipped_items = 0
        valid_items_count = 0

        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            item_id = str(entry.get("item_id") or "").strip()
            result_payload = entry.get("result", entry)
            if not isinstance(result_payload, Mapping):
                continue

            result_dict = dict(result_payload)
            # Normalize keywords if strings or alternative keys are provided
            if isinstance(result_dict.get("keywords"), list):
                norm_kw = []
                for k in result_dict["keywords"]:
                    if isinstance(k, str):
                        norm_kw.append({"term_en": k})
                    elif isinstance(k, Mapping):
                        k_copy = dict(k)
                        if "term_en" not in k_copy and "keyword" in k_copy:
                            k_copy["term_en"] = k_copy.pop("keyword")
                        norm_kw.append(k_copy)
                    else:
                        norm_kw.append(k)
                result_dict["keywords"] = norm_kw

            try:
                extraction = ExtractionResult.model_validate(result_dict)
            except Exception as exc:
                raise ValueError(f"条目 {item_id} 数据结构不符合 ExtractionResult Schema: {exc}") from exc

            valid_items_count += 1
            if extraction.skip:
                skipped_items += 1
                continue

            bundle = bundle_map.get(item_id)
            if bundle is None:
                # If bundle cannot be resolved by ID, create a fallback minimal bundle
                bundle = ContextBundle(
                    doc_id=doc_key,
                    section_path=str(doc.get("title") or ""),
                    focal_sentence="Agent extraction entry",
                )

            for draft in extraction.cards:
                if hasattr(draft, "structured_details"):
                    struct = dict(draft.structured_details or {})
                    struct["extractor_model"] = agent_model or "CLI_AGENT"
                    draft.structured_details = struct
                try:
                    stored_id = self.pipeline._store_draft(
                        draft,
                        doc,
                        bundle,
                        extraction.keywords,
                        provenance_override=provenance_override,
                        provenance_source="agent_cli",
                    )
                except TypeError:
                    stored_id = self.pipeline._store_draft(
                        draft, doc, bundle, extraction.keywords
                    )
                if stored_id:
                    stored_cards += 1

            stored_keywords += len(extraction.keywords)

        # Update document parse status
        self.db.update_parse_status(doc_key, "LLM_EXTRACTED")
        self.db.log_event(
            "INFO",
            "agent_submit",
            f"{doc_key} Agent 提炼结果入库：{stored_cards} 张知识卡片、{stored_keywords} 个关键词",
            doc_id=doc_key,
        )

        return {
            "ok": True,
            "doc_id": doc_key,
            "processed_items": valid_items_count,
            "stored_cards": stored_cards,
            "stored_keywords": stored_keywords,
            "skipped_items": skipped_items,
        }

    def get_task_status(self, doc_id: str) -> dict[str, Any]:
        """Check status of agent task bundle and extraction for a document."""
        doc_key = str(doc_id or "").strip()
        doc = self.db.get_document(doc_key)
        if not doc:
            raise ValueError(f"找不到文献：{doc_key}")

        task_dir = self.workspace / "agent_tasks" / doc_key
        has_task_dir = task_dir.is_dir()
        items_count = 0
        if has_task_dir and (task_dir / "items.json").is_file():
            try:
                items_count = len(json.loads((task_dir / "items.json").read_text(encoding="utf-8")))
            except Exception:
                pass

        result_file = task_dir / "result.json" if has_task_dir else None
        has_result = result_file.is_file() if result_file else False

        if hasattr(self.db, "cards_for_document"):
            cards = self.db.cards_for_document(doc_key)
        elif hasattr(self.db, "list_knowledge_cards"):
            cards = self.db.list_knowledge_cards(filters={"doc_id": doc_key}, limit=500)
        else:
            cards = []

        return {
            "ok": True,
            "doc_id": doc_key,
            "title": str(doc.get("title") or ""),
            "parse_status": str(doc.get("parse_status") or ""),
            "has_task_bundle": has_task_dir,
            "task_dir": str(task_dir.resolve()) if has_task_dir else "",
            "item_count": items_count,
            "has_result_json": has_result,
            "stored_cards_count": len(cards),
        }

    @staticmethod
    def _build_instructions_markdown(
        *,
        doc: Mapping[str, Any],
        doc_key: str,
        item_count: int,
        tables_catalog: list[dict[str, str]],
        has_images: bool,
        task_dir: Path,
    ) -> str:
        title = str(doc.get("title") or "Unknown Title")
        journal = str(doc.get("journal") or "—")
        year = str(doc.get("pub_year") or "—")
        doi = str(doc.get("doi") or "—")
        authors = ", ".join(doc.get("authors_list") or ["Unknown"])

        tables_list_md = (
            "\n".join(
                f"- `{t['file']}`: {t['caption']}" for t in tables_catalog
            )
            if tables_catalog
            else "（本文献无独立数据表）"
        )

        images_sec = (
            "4. `images/` 目录：存放从文献裁切的高清插图文件。若条目包含 `image_files`，可按路径查看。\n"
            if has_images
            else ""
        )

        return f"""# 原子物理计算知识提取任务指引 ({doc_key})

你是一名资深原子物理计算与理论光谱学专家（精通 MCDHF、RCI、MBPT、FAC、GRASP 等计算方法）。
请基于本任务目录下的文献材料，提炼原子物理计算参数、物理模型、精度评估与避坑指南，生成严格结构化的结果文件。

---

## 一、文献基本信息
- **DOC-ID**: `{doc_key}`
- **论文标题**: {title}
- **期刊/年份**: {journal} ({year})
- **作者**: {authors}
- **DOI**: {doi}

---

## 二、目录文件清单与使用指南
本目录（`{task_dir.resolve()}`）已为您准备好以下预处理材料：

1. `items.json` (**【核心提取任务】**):
   包含由四通道雷达扫描出的 **{item_count} 个物理提炼条目**。
   每个条目包含：
   - `item_id`: 条目唯一标识符（如 `{doc_key}-0`）；
   - `section_path`: 所在章节路径；
   - `focal_sentence` & `focal_window`: 物理陈述核心句与带标记的上下文窗口；
   - `backtraced_definitions`: 回溯的符号/代号定义；
   - `table_refs` & `table_files`: 该条目引用的表格文件清单（见下方）。
2. `schema.json` (**【输出格式规范】**):
   定义了最终输出文件必须满足的 JSON Schema 规范。每个条目的 `result` 对应系统的 `ExtractionResult` 结构。
3. `parsed.md` (**【全篇 Markdown 原文】**):
   文献经版面分析与 OCR 还原的完整 Markdown 文本。包含规范的 LaTeX 数学公式。若对条目上下文有疑义，可随时检索查阅。
4. `tables/` 目录 (**【独立数据表格】**):
   骨架脱水后的独立表格，已保留表头、前 5 行典型数据与文末表注：
{tables_list_md}
{images_sec}
---

## 三、你的提炼目标与物理维度
针对 `items.json` 中的每一个条目，请深入提炼以下关键物理信息：
1. **计算模型与哈密顿量 (MODEL)**：如 Dirac-Coulomb、Breit 相互作用、QED 辐射校正、极化势等；
2. **计算策略与活性空间 (STRAT)**：CSFs 生成策略、活性集扩展层级（如 $n=3,4,5$）、核-价 (CV) 与价-价 (VV) 相关能收敛规律；
3. **关键数据结论 (CONC)**：能级差、跃迁波长、跃迁率 $A$、振子强度 $gf$、激发态寿命 $\\tau$ 与实验值或 NIST 库的精度比较（相对误差 %）；
4. **避坑与经验陷阱 (PITFALL)**：如不收敛、数值不稳定、规范依赖性（Length vs Velocity Gauge）、态标记混合/混乱等；
5. **程序与算法 (CODE)**：如 GRASP2K、FAC、AMBiT、MCDFGME 等软件包的使用参数或脚本模式。

---

## 四、输出与提交规范（严格遵守）
1. **文件输出路径**：
   请将最终提取结果保存为当前目录下的 **`result.json`**：
   `{task_dir.resolve() / "result.json"}`
2. **根结构必须满足**：
   ```json
   {{
     "items": [
       {{
         "item_id": "{doc_key}-0",
         "result": {{
           "cards": [
             {{
               "title": "简明清晰的知识卡片标题",
               "category": "CONC",
               "element": "Fe",
               "charge_state": 16,
               "method": "RCI",
               "program": "GRASP2K",
               "summary": "详细物理结论阐述...",
               "uncertainty": "平均相对偏差 2.2%",
               "formulas_latex": "$$ H_{{\\\\text{{Breit}}}} = -\\\\sum_{{i < j}} ... $$"
             }}
           ],
           "keywords": [
             {{"keyword": "Ne-like Fe XVII", "category": "ION"}}
           ],
           "notes": "提炼说明或备注",
           "skip": false
         }}
       }}
     ]
   }}
   ```
3. **特别转义要求**：
   JSON 内部的所有 LaTeX 数学公式反斜杠**必须双写转义**（例如写为 `\\\\frac`, `\\\\alpha`, `\\\\langle`, `\\\\beta`，绝对不要写单反斜杠 `\\`）。
4. **完成后的提交验证命令**：
   在保存 `result.json` 后，您可以通过执行以下命令进行本地验证与入库：
   ```bash
   python -m app_core agent submit {doc_key} result.json
   ```
"""


__all__ = ["AgentTaskPackager"]
