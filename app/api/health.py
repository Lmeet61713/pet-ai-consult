"""健康检查（v5 §10.4）

/health/live 只查进程存活；/health/ready 检查 Redis、vLLM 必要依赖，
不真实调用 DeepSeek（避免探测流量计费）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.dependencies import get_container

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict:
    return {"status": "alive"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    """ready 检查 Redis + VisionGateway（v6.3 §10.4：不真实调用 KnowledgeConsult）。"""
    container = get_container(request)
    guard_required = container.settings.guard_enforced
    checks: dict[str, bool] = {
        "redis": False,
        "vision_gateway": False,
        "guard": not guard_required,
    }
    if container.redis is not None:
        checks["redis"] = await container.redis.ping()
    if container.vision_client is not None:
        checks["vision_gateway"] = await container.vision_client.ping()
    if guard_required and container.guard_client is not None:
        checks["guard"] = await container.guard_client.ping()
    healthy = all(checks.values())
    shadow_checks = {
        "rag_cards": (
            not container.settings.rag_shadow
            or (
                container.rag_retriever is not None
                and container.rag_retriever.report.ready
            )
        ),
        "rag_emergency_rules": (
            not container.settings.rag_shadow
            or not container.settings.rag_emergency_shadow
            or (
                container.rag_emergency_matcher is not None
                and container.rag_emergency_matcher.report.ready
            )
        ),
    }
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ready" if healthy else "degraded",
            "checks": checks,
            "shadow_checks": shadow_checks,
        },
    )
