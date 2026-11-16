"""Backfill model provenance for knowledge cards created before V2.4.0."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_CORE = ROOT / "app_core"
if str(APP_CORE) not in sys.path:
    sys.path.insert(0, str(APP_CORE))

from db.sqlite_manager import SQLiteManager  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="给已有知识卡补写 card_model_provenance。"
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="包含 atomic_meta.sqlite 的 workspace 目录",
    )
    parser.add_argument("--model", required=True, help="模型名，例如 deepseek-v4.1-flash-1017")
    parser.add_argument(
        "--roles",
        default="fast_llm",
        help="逗号分隔的角色，默认 fast_llm；可选 slow_llm,vlm,embedding,reranker",
    )
    parser.add_argument("--node-id", default="", help="节点 ID，默认使用模型名")
    parser.add_argument("--node-name", default="", help="节点显示名")
    parser.add_argument("--node-type", default="OPENAI_COMPATIBLE_API")
    parser.add_argument("--provider", default="api")
    parser.add_argument("--source", default="legacy_backfill")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="覆盖已有同角色 provenance；默认跳过已有记录",
    )
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 张卡，0=全部")
    args = parser.parse_args()

    workspace = Path(args.workspace)
    db = SQLiteManager(workspace)
    try:
        roles = [part.strip() for part in args.roles.split(",") if part.strip()]
        rows = db.conn.execute(
            "SELECT knw_id FROM knowledge_cards ORDER BY knw_id"
        ).fetchall()
        if args.limit:
            rows = rows[: max(1, int(args.limit))]
        updated = 0
        skipped = 0
        node = {
            "node_id": args.node_id or args.model,
            "name": args.node_name or f"{args.model} API",
            "node_type": args.node_type,
            "provider": args.provider,
            "model": args.model,
        }
        for row in rows:
            knw_id = str(row["knw_id"])
            existing = db.provenance_for_card(knw_id)
            for role in roles:
                if role in existing and not args.overwrite:
                    skipped += 1
                    continue
                db.set_card_provenance(
                    knw_id, role, node, source=args.source
                )
                updated += 1
        print(
            f"cards={len(rows)} roles={','.join(roles)} updated={updated} "
            f"skipped={skipped} db={db.db_path}"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
