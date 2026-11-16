"""Nightly Markdown report generator."""

from __future__ import annotations

import logging
from datetime import date as date_cls, datetime, time, timedelta
from pathlib import Path
from typing import Any

from db.sqlite_manager import SQLiteManager, now_iso

log = logging.getLogger(__name__)


class MorningReporter:
    def __init__(self, db: SQLiteManager, workspace_dir: str | Path):
        self.db = db
        self.workspace = Path(workspace_dir)
        self.report_dir = self.workspace / "morning_reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self,
        *,
        date: date_cls | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> Path:
        day = date or datetime.now().astimezone().date()
        until = until or now_iso()
        if since is None:
            start_dt = datetime.combine(day, time(0, 0)).astimezone() - timedelta(hours=12)
            since = start_dt.isoformat(timespec="seconds")
        documents = self.db.documents_updated_between(since, until)
        cards = self.db.cards_updated_between(since, until)
        conflicts = [card for card in cards if int(card.get("has_conflict") or 0)]
        manual = self.db.list_documents(download_status="MANUAL_REQUIRED", limit=200)
        failed_tasks = self.db.list_tasks("FAILED", limit=100)
        stats = self.db.stats()

        lines: list[str] = [
            f"# 工作台运行简报 · {day.isoformat()}",
            "",
            f"> 生成于 {until} · 时间窗 {since} → {until}",
            "",
            "## 运行概览",
            "",
            "| 指标 | 数量 |",
            "| :--- | ---: |",
            f"| 文献总数 | {stats['documents']} |",
            f"| 知识卡片 | {stats['cards']}（L1 总纲 {stats['l1_cards']}） |",
            f"| 待处理任务 | {stats['pending_tasks']} |",
            f"| 学术争议 | {stats['conflicts']} |",
            f"| 待手动导入 | {stats['manual_required']} |",
            f"| 解析失败 | {stats['failed_parse']} |",
            "",
            "## 新增 / 更新知识卡",
            "",
        ]
        if cards:
            for card in cards:
                lines.append(
                    f"- **{card['knw_id']}** · {card.get('title_zh', '')} "
                    f"（confidence/consensus={card.get('consensus_count', 1)}，"
                    f"authority={card.get('authority_score', 1.0):.2f}）"
                )
                if card.get("summary_zh"):
                    lines.append(f"  - {card['summary_zh'][:320]}")
        else:
            lines.append("- 本时间窗没有新增或更新卡片。")

        lines.extend(["", "## ⚡ 学术争议", ""])
        if conflicts:
            for card in conflicts:
                lines.append(
                    f"- **{card['knw_id']}** · {card.get('title_zh', '')} "
                    f"↔ {', '.join(card.get('conflict_with_knw_ids_list') or []) or '待关联'}"
                )
                if card.get("premises_and_conditions"):
                    lines.append(f"  - 前提：{card['premises_and_conditions'][:300]}")
        else:
            lines.append("- 未发现极性冲突。")

        lines.extend(["", "## 📥 待手动下载", ""])
        if manual:
            for doc in manual[:30]:
                doi = doc.get("doi") or ""
                link = f"https://doi.org/{doi}" if doi else doc.get("source_url") or ""
                suffix = f" · [DOI]({link})" if link else ""
                lines.append(f"- **{doc['doc_id']}** · {doc['title']}{suffix}")
        else:
            lines.append("- 队列为空。")

        lines.extend(["", "## 失败与重试", ""])
        if failed_tasks:
            for task in failed_tasks[:30]:
                lines.append(
                    f"- `{task['task_type']}` · {task.get('doc_id') or '—'} · "
                    f"attempts={task.get('attempts')} · {task.get('last_error') or '—'}"
                )
        else:
            lines.append("- 没有失败任务。")

        suggestions: list[str] = []
        if conflicts:
            suggestions.append("优先在工作台跨文献深度归纳中标记为 ⚡ 的卡片对，检查前提差异。")
        if manual:
            suggestions.append("把已下载的 PDF 拖入文献库，或放入 workspace/inbox_manual 自动匹配入库。")
        if failed_tasks:
            suggestions.append("重试失败任务前先查看 error_log，避免重复消耗下载配额。")
        if not suggestions:
            suggestions.append("继续扩展活性空间/方法维度的文献覆盖。")
        lines.extend(["", "## 建议的下一步", ""])
        lines.extend(f"- {item}" for item in suggestions)
        lines.append("")

        target = self.report_dir / f"Report_{day.strftime('%Y%m%d')}.md"
        target.write_text("\n".join(lines), encoding="utf-8")
        self.db.log_event("INFO", "report", f"生成工作台运行简报 {target.name}")
        log.info("Morning report written to %s", target)
        return target

    def latest_report(self) -> Path | None:
        reports = sorted(self.report_dir.glob("Report_*.md"))
        return reports[-1] if reports else None

    def read(self, day: date_cls | None = None) -> str:
        if day is None:
            path = self.latest_report()
        else:
            path = self.report_dir / f"Report_{day.strftime('%Y%m%d')}.md"
        if path is None or not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
