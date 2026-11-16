#!/usr/bin/env python3
"""Generate a Markdown reference for the executable SQLite schema.

The runtime schema lives in ``app_core/db/schema_v2.sql`` and is executed by
SQLite directly.  Codebase-memory's generic SQL grammar does not fully parse
SQLite-specific FTS5 virtual tables and triggers, so this script executes the
schema in an in-memory database and exports the effective tables, columns,
foreign keys, indexes and triggers as Markdown.

Usage:
    python scripts/export_schema_reference.py
    python scripts/export_schema_reference.py --check   # fail if stale
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "app_core" / "db" / "schema_v2.sql"
DEFAULT_OUTPUT = ROOT / "docs" / "schema_reference.md"


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _md(value: Any) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text.replace("|", "\\|")


def _rows(conn: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return list(conn.execute(sql).fetchall())


def _is_fts_shadow(name: str, fts_tables: Iterable[str]) -> bool:
    return any(name.startswith(f"{base}_") for base in fts_tables) and name not in set(fts_tables)


def _table_block(conn: sqlite3.Connection, table: sqlite3.Row) -> list[str]:
    name = str(table["name"])
    lines = [f"### `{name}`", ""]

    columns = _rows(conn, f"PRAGMA table_info({_ident(name)})")
    lines.extend(
        [
            "| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |",
            "|---:|---|---|:---:|---|---|",
        ]
    )
    for column in columns:
        lines.append(
            "| {cid} | `{name}` | {type} | {notnull} | {default} | {pk} |".format(
                cid=column["cid"],
                name=_md(column["name"]),
                type=_md(column["type"]) or "—",
                notnull="是" if column["notnull"] else "否",
                default=f"`{_md(column['dflt_value'])}`" if column["dflt_value"] is not None else "—",
                pk=str(column["pk"]) if column["pk"] else "—",
            )
        )

    foreign_keys = _rows(conn, f"PRAGMA foreign_key_list({_ident(name)})")
    if foreign_keys:
        lines.extend(["", "**外键**", "", "| 列 | 引用表 | 引用列 | ON DELETE | ON UPDATE |", "|---|---|---|---|---|"])
        for fk in foreign_keys:
            lines.append(
                f"| `{_md(fk['from'])}` | `{_md(fk['table'])}` | `{_md(fk['to'])}` | "
                f"{_md(fk['on_delete'])} | {_md(fk['on_update'])} |"
            )

    indexes = _rows(conn, f"PRAGMA index_list({_ident(name)})")
    if indexes:
        lines.extend(["", "**索引**", "", "| 索引 | 列 | 唯一 | 来源 | 部分索引 |", "|---|---|:---:|---|---|"])
        for index in indexes:
            index_name = str(index["name"])
            index_columns = _rows(conn, f"PRAGMA index_info({_ident(index_name)})")
            column_names = ", ".join(f"`{_md(item['name'])}`" for item in index_columns) or "—"
            origin = {"c": "CREATE INDEX", "u": "UNIQUE 约束", "pk": "主键"}.get(
                str(index["origin"]), str(index["origin"])
            )
            lines.append(
                f"| `{_md(index_name)}` | {column_names} | "
                f"{'是' if index['unique'] else '否'} | {origin} | "
                f"{'是' if index['partial'] else '否'} |"
            )

    ddl = str(table["sql"] or "").strip()
    if ddl:
        lines.extend(["", "<details><summary>原始 DDL</summary>", "", "```sql", ddl, "```", "", "</details>"])
    lines.append("")
    return lines


def build_markdown(schema_path: Path) -> str:
    schema_text = schema_path.read_text(encoding="utf-8")
    schema_hash = hashlib.sha256(schema_text.encode("utf-8")).hexdigest()

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(schema_text)
        objects = _rows(
            conn,
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """,
        )
        fts_tables = [
            str(item["name"])
            for item in objects
            if item["type"] == "table"
            and item["sql"]
            and "using fts5" in str(item["sql"]).lower()
        ]
        tables = [
            item
            for item in objects
            if item["type"] == "table" and not _is_fts_shadow(str(item["name"]), fts_tables)
        ]
        triggers = [item for item in objects if item["type"] == "trigger"]

        total_columns = 0
        total_indexes = 0
        table_sections: list[str] = []
        for table in tables:
            table_sections.extend(_table_block(conn, table))
            total_columns += len(_rows(conn, f"PRAGMA table_info({_ident(str(table['name']))})"))
            total_indexes += len(_rows(conn, f"PRAGMA index_list({_ident(str(table['name']))})"))

        lines = [
            "# SQLite Schema Reference",
            "",
            "> 本文件由 `scripts/export_schema_reference.py` 自动生成，请勿手工编辑。",
            "> 运行时唯一 schema 源是 `app_core/db/schema_v2.sql`；本文件只是便于检索和代码记忆的派生文档。",
            "",
            f"- Schema 文件：`app_core/db/schema_v2.sql`",
            f"- SHA256：`{schema_hash}`",
            f"- 用户表：**{len(tables)}**",
            f"- 列：**{total_columns}**",
            f"- 索引：**{total_indexes}**（含 SQLite 自动索引）",
            f"- 触发器：**{len(triggers)}**",
            f"- FTS5 虚拟表：{', '.join(f'`{name}`' for name in fts_tables) or '无'}",
            "",
            "## 关系概览",
            "",
            "- `documents` 1 → N `raw_evidence_chunks` / `tasks`",
            "- `knowledge_cards` 1 → N `raw_evidence_chunks` / `card_keywords` / `card_model_provenance` / `card_audit_reviews` / `card_revisions`",
            "- `controlled_keywords` 1 → N `card_keywords`",
            "- `knowledge_fts` 是 `knowledge_cards` 的外部内容 FTS5 索引，由 3 个触发器维护",
            "",
            "## 表结构",
            "",
        ]
        lines.extend(table_sections)

        lines.extend(["## 触发器", ""])
        if triggers:
            for trigger in triggers:
                lines.extend(
                    [
                        f"### `{trigger['name']}`",
                        "",
                        f"- 表：`{trigger['tbl_name']}`",
                        "",
                        "```sql",
                        str(trigger["sql"] or "").strip(),
                        "```",
                        "",
                    ]
                )
        else:
            lines.extend(["无。", ""])

        # ``created_at`` is written at database creation time and would make
        # this generated document change on every run.  Keep the reference
        # deterministic; the runtime still records the real timestamp.
        system_meta = _rows(
            conn,
            "SELECT key, value FROM system_meta WHERE key <> 'created_at' ORDER BY key",
        )
        lines.extend(["## system_meta 初始值", "", "| key | value |", "|---|---|"])
        for item in system_meta:
            lines.append(f"| `{_md(item['key'])}` | `{_md(item['value'])}` |")
        lines.append("")
        lines.append("`created_at` 在数据库创建时由运行时写入，不列入本表。")
        lines.append("")

        shadow_tables = [
            str(item["name"])
            for item in objects
            if item["type"] == "table" and _is_fts_shadow(str(item["name"]), fts_tables)
        ]
        if shadow_tables:
            lines.extend(
                [
                    "## FTS5 影子表",
                    "",
                    "以下表由 SQLite FTS5 自动维护，不应直接读写：",
                    "",
                    "```text",
                    "\n".join(shadow_tables),
                    "```",
                    "",
                ]
            )

        return "\n".join(lines).rstrip() + "\n"
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="只检查输出是否为最新，不写文件")
    args = parser.parse_args()

    schema_path = args.schema if args.schema.is_absolute() else ROOT / args.schema
    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    try:
        rendered = build_markdown(schema_path)
    except sqlite3.Error as exc:
        print(f"schema 执行失败：{exc}", file=sys.stderr)
        return 1

    if args.check:
        current = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        if current != rendered:
            print(f"{output_path} 已过期；请运行 python scripts/export_schema_reference.py", file=sys.stderr)
            return 1
        print(f"{output_path} 已是最新")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered, encoding="utf-8")
    print(f"wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
