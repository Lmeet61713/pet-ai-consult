#!/usr/bin/env python
"""request_id 穿透查询（效果评审工具，v1.4 §7.3）。

用法：
  python scripts/query_dialogue.py --id req_xxx              # 单条完整链路
  python scripts/query_dialogue.py --keyword 拉稀 --limit 20 # 关键词批量
  python scripts/query_dialogue.py --recent 50              # 最近 N 条摘要

数据源：PG consult_dialogue（队列化启用时）；无 PG 时回退 runtime/dialogue.jsonl。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _fmt_one(d: dict) -> str:
    lines = [
        f"[{d.get('request_id')}] {d.get('status')} mode={d.get('answer_mode')} risk={d.get('risk_level')}",
        f"  时间: {d.get('ts') or d.get('created_at')}  用户: {d.get('user_id')}  会话: {d.get('conversation_id')}",
        f"  提问: {d.get('user_text') or '(空)'}",
        f"  命中卡片: {d.get('hit_card_ids')}  RAG: {d.get('rag_decision')}",
        f"  降级: {d.get('degraded_services')}  追问: {d.get('follow_up_questions')}",
        f"  耗时: {d.get('total_ms')}ms",
    ]
    answer = d.get("answer") or ""
    lines.append("  回答:")
    for line in answer.split("\n")[:12]:
        lines.append(f"    {line[:100]}")
    return "\n".join(lines)


def _load_pg(database_url: str) -> list[dict]:
    import asyncio

    from sqlalchemy import select

    from app.tasks.db import build_engine, build_session_factory
    from app.tasks.models import ConsultDialogue

    async def load():
        engine = build_engine(database_url)
        factory = build_session_factory(engine)
        async with factory() as session:
            rows = list((await session.scalars(select(ConsultDialogue))).all())
        await engine.dispose()
        return [
            {c.name: getattr(r, c.name) for c in ConsultDialogue.__table__.columns}
            for r in rows
        ]

    return asyncio.run(load())


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="对话效果查询")
    parser.add_argument("--id", help="request_id 精确查询")
    parser.add_argument("--keyword", help="用户提问关键词")
    parser.add_argument("--recent", type=int, help="最近 N 条摘要")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--db", default="", help="PG 连接串（空则自动读 .env）")
    parser.add_argument("--jsonl", default="runtime/dialogue.jsonl", help="JSONL 路径")
    args = parser.parse_args()

    rows: list[dict] = []
    database_url = args.db
    if not database_url:
        from app.core.config import Settings

        database_url = Settings().consult_database_url
    try:
        if database_url:
            rows = _load_pg(database_url)
    except Exception as exc:  # noqa: BLE001 - 回退 JSONL
        print(f"[warn] PG 查询失败，回退 JSONL: {exc}", file=sys.stderr)
    if not rows:
        rows = _load_jsonl(Path(args.jsonl))
    if not rows:
        print("无数据（PG 与 JSONL 均为空）")
        return 1

    rows.sort(key=lambda d: str(d.get("ts") or d.get("created_at") or ""), reverse=True)
    if args.id:
        hit = [d for d in rows if d.get("request_id") == args.id]
        if not hit:
            print(f"未找到 request_id={args.id}")
            return 1
        print(_fmt_one(hit[0]))
        return 0
    if args.keyword:
        rows = [d for d in rows if args.keyword in (d.get("user_text") or "")]
    elif args.recent:
        rows = rows[: args.recent]
    else:
        rows = rows[: args.limit]
    for d in rows:
        print("-" * 60)
        print(_fmt_one(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())