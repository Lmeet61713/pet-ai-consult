"""对话存档（效果观测 v1.2 §7，Phase 1 JSONL 轻量版）。

每请求一行 JSON 追加到配置的路径：输入、中间决策、输出、分段耗时、降级标记。
- 异步写：不在响应关键路径上阻塞；写失败仅告警，不影响本轮返回。
- 空路径 = 关闭（测试默认关）。Phase 2 队列化后迁移到 PostgreSQL consult_dialogue 表。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class DialogueArchive:
    """JSONL 追加式对话存档。"""

    def __init__(self, path: str | None = None):
        self.path = path or ""
        self._write_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    async def write(self, record: dict[str, Any]) -> None:
        """异步追加一行 JSON；任何失败只记 warning（不阻断问诊）。"""
        if not self.path:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            await self._append(line)
        except Exception:  # noqa: BLE001 - 存档失败不影响主链路
            logger.warning("dialogue_archive_write_failed", exc_info=True)

    async def _append(self, line: str) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._append_sync, line)

    def _append_sync(self, line: str) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")