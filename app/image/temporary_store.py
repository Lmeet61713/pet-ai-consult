"""请求级临时图片存储（v5 §11.2）

- 临时目录按 request_id 隔离；
- 进程异常时由定时清理任务过期目录；
- 请求结束（agent finally）调用 cleanup() 立即删除。
第一版图片走内存模式（小图，≤5MB 压缩后更小），本模块为超大图/临时文件
场景预留，并提供目录隔离与清理接口。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_TTL_SECONDS = 3600
_SWEEP_INTERVAL_SECONDS = 600


class TemporaryImageStore:
    def __init__(self, base_dir: str | None = None):
        self._base = Path(base_dir) if base_dir else Path(tempfile.gettempdir()) / "pet-consult-images"
        self._base.mkdir(parents=True, exist_ok=True)
        self._sweeper: asyncio.Task | None = None

    def dir_for(self, request_id: str) -> Path:
        d = self._base / request_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def cleanup(self, request_id: str) -> None:
        shutil.rmtree(self._base / request_id, ignore_errors=True)

    async def start_sweeper(self) -> None:
        """定时清理过期请求目录（进程异常兜底）。"""
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
            try:
                now = time.time()
                for d in self._base.iterdir():
                    if d.is_dir() and now - d.stat().st_mtime > _TTL_SECONDS:
                        shutil.rmtree(d, ignore_errors=True)
                        logger.info("清理过期临时目录: %s", d.name)
            except Exception:  # noqa: BLE001
                logger.exception("临时目录清理异常")

    async def close(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
