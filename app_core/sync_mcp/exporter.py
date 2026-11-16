"""Human-facing exports: manual-download checklist and document CSV (V2)."""

from __future__ import annotations

import csv
import html
from datetime import datetime
from pathlib import Path

from db.sqlite_manager import SQLiteManager


def export_pending_html(db: SQLiteManager, workspace_dir: str | Path) -> Path:
    workspace = Path(workspace_dir)
    documents = db.list_documents(download_status="MANUAL_REQUIRED", limit=2000)
    out_dir = workspace / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"pending_downloads_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    rows = []
    for doc in documents:
        doi = doc.get("doi") or ""
        link = f"https://doi.org/{doi}" if doi else (doc.get("source_url") or "")
        title = html.escape(doc["title"])
        if link:
            title = f'<a href="{html.escape(link)}" target="_blank">{title}</a>'
            doi_cell = (
                f'<a href="{html.escape(link)}" target="_blank">'
                f"{html.escape(doi or link)}</a>"
            )
        else:
            doi_cell = "—"
        rows.append(
            "<tr>"
            f'<td><code>{html.escape(doc["doc_id"])}</code></td>'
            f'<td class="title">{title}</td>'
            f'<td>{html.escape(", ".join(doc.get("authors_list") or []))}</td>'
            f'<td>{html.escape(doc.get("journal") or "—")}</td>'
            f'<td>{doc.get("pub_year") or "—"}</td>'
            f"<td>{doi_cell}</td>"
            "</tr>"
        )
    body = "\n".join(rows) or (
        '<tr><td colspan="6" class="empty">当前没有需要手动下载的文献。</td></tr>'
    )
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AtomicCalculationDB · 待下载清单</title>
<style>
body {{ margin:0; padding:40px; background:#F8FAFD; color:#1B1B1F;
       font-family:"Segoe UI","Microsoft YaHei UI",sans-serif; }}
h1 {{ font-size:26px; margin:0 0 6px; }}
p.sub {{ color:#5F6368; margin:0 0 24px; }}
table {{ width:100%; border-collapse:separate; border-spacing:0; background:#fff;
         border-radius:20px; overflow:hidden; box-shadow:0 4px 16px rgba(0,0,0,.04); }}
th,td {{ padding:13px 16px; text-align:left; font-size:13px; border-bottom:1px solid #EEF1F6; }}
th {{ background:#EFF3F8; color:#44474E; }}
tr:last-child td {{ border-bottom:none; }}
code {{ background:#D3E3FD; color:#0B57D0; border-radius:8px; padding:2px 7px;
        font-family:Consolas,monospace; font-size:12px; }}
a {{ color:#0B57D0; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
td.title {{ max-width:420px; }}
.empty {{ text-align:center; color:#5F6368; padding:40px; }}
</style></head><body>
<h1>待手动下载清单</h1>
<p class="sub">共 {len(documents)} 篇 · 生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")} ·
下载后拖入 AtomicCalculationDB 或放入 workspace/inbox_manual 自动导入并解析</p>
<table><thead><tr><th>DOC-ID</th><th>标题</th><th>作者</th><th>期刊</th><th>年份</th><th>DOI / 链接</th></tr></thead>
<tbody>{body}</tbody></table>
</body></html>"""
    target.write_text(page, encoding="utf-8")
    db.log_event("INFO", "export", f"导出待下载清单: {target.name}")
    return target


def export_documents_csv(db: SQLiteManager, workspace_dir: str | Path) -> Path:
    workspace = Path(workspace_dir)
    out_dir = workspace / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"documents_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    documents = db.list_documents(limit=2000)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["doc_id", "type", "year", "title", "authors", "journal", "doi", "download", "parse"]
        )
        for doc in documents:
            writer.writerow(
                [
                    doc["doc_id"],
                    doc.get("doc_type", ""),
                    doc.get("pub_year", ""),
                    doc.get("title", ""),
                    "; ".join(doc.get("authors_list") or []),
                    doc.get("journal", ""),
                    doc.get("doi", ""),
                    doc.get("download_status", ""),
                    doc.get("parse_status", ""),
                ]
            )
    return target
