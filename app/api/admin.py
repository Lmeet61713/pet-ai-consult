"""内部诊断接口（v5 §5 api/admin.py：默认关闭公网，ENABLE_ADMIN_API=true 才挂载）"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.core.config import Settings
from app.core.dependencies import get_app_settings, get_container
from app.core.exceptions import UnauthorizedError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)


async def _admin_guard(settings: Settings = Depends(get_app_settings)) -> None:
    if not settings.enable_admin_api:
        raise UnauthorizedError("Admin API 未启用")


@router.get("/diagnostics", dependencies=[Depends(_admin_guard)])
async def diagnostics(request: Request) -> dict:
    container = get_container(request)
    return {
        "env": container.settings.app_env,
        "mock_mode": container.settings.mock_mode,
        "guard_mode": container.settings.guard_mode,
        "rag_mode": container.settings.rag_mode,
        "rag_loaded": bool(container.rag_retriever and container.rag_retriever.report.ready),
        "enable_guard_legacy": container.settings.enable_guard,
        "redis_ok": await container.redis.ping() if container.redis else False,
        "vision_mock": container.vision_client.is_mock if container.vision_client else None,
        "deepseek_mock": container.consultation_service.knowledge_consult.adapter.is_mock,
        "guard_mock": container.guard_client.is_mock if container.guard_client else None,
    }
