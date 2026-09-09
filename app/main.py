"""
宠物问诊咨询系统 - FastAPI 应用入口

负责：
- 应用生命周期管理（启动/关闭）
- 依赖容器初始化（Redis、视觉、问诊、审核等客户端）
- 中间件、异常处理器、路由注册
- 生产环境安全校验（禁止 Mock、强制 JWT 密钥等）

版本：v7.3
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.contract_store import ContractStore
from app.core.dependencies import Container
from app.core.error_handlers import install_error_handlers
from app.core.logging import setup_logging
from app.core.middleware import RequestContextMiddleware

logger = logging.getLogger(__name__)


def validate_runtime_settings(settings: Settings) -> None:
    """运行时安全校验（V1.1 P0-4）：环境显式配置；生产漏配/不一致拒绝启动。

    校验规则：
    - APP_ENV 必填（config 无默认值，缺失时 Settings 构造即失败）
    - 生产：禁止任何 MOCK_* 开关、只监听本机、Admin API 拒绝
    - 非生产：禁止监听公网地址
    - 非 Mock 模式必须配置 JWT_SIGNING_SECRET
    - 生产环境必须配置 REDIS_PASSWORD
    """
    # 只要关闭全局 Mock，鉴权就会真实生效；不能只在 production 才校验密钥，
    # 否则 test/development 经 Nginx 暴露时可使用空密钥签发 HS256 Token。
    # 2026-08-21: AUTH_SKIP=true（内部部署免 JWT）时同样跳过密钥校验。
    if not settings.mock_mode and not settings.auth_skip:
        if not settings.jwt_signing_secret or settings.jwt_signing_secret == "change-me":
            raise RuntimeError("非 MOCK_MODE 必须配置 JWT_SIGNING_SECRET")

    if settings.app_env == "production":
        if any(
            (
                settings.mock_mode,
                settings.mock_vision,
                settings.mock_knowledge_consult,
                settings.mock_guard,
            )
        ):
            raise RuntimeError("生产环境禁止任何 MOCK_* 开关")
        if settings.app_host not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("FastAPI 生产实例只能监听本机，由 Nginx 对外暴露")
        if not settings.redis_password:
            raise RuntimeError("生产环境必须配置 REDIS_PASSWORD")
        if settings.knowledge_provider == "local_openai":
            if not settings.knowledge_api_base_url:
                raise RuntimeError("生产环境本地模型必须配置 KNOWLEDGE_API_BASE_URL")
        elif not settings.knowledge_api_base_url or not settings.knowledge_api_key:
            raise RuntimeError(
                "生产环境外部模型必须配置 KNOWLEDGE_API_BASE_URL 和 KNOWLEDGE_API_KEY"
            )
        if settings.enable_admin_api:
            raise RuntimeError("生产环境禁止开启 ENABLE_ADMIN_API")
        if (
            settings.knowledge_provider == "deepseek_official"
            and not ContractStore(settings).load_verified_contract()
        ):
            raise RuntimeError(
                "知识契约未验证或不匹配当前配置：请先运行 "
                "scripts/verify_deepseek_contract.py --confirm"
            )
    elif settings.app_host in {"0.0.0.0", "::"}:
        raise RuntimeError("非生产环境禁止监听公网地址")


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    """FastAPI 应用工厂函数。

    使用工厂模式而非模块级 app 实例，确保环境配置缺失时在请求到达前即拒绝启动。
    container 参数用于测试复用已注入依赖的容器（跳过重复 startup）。

    启动方式：
        uvicorn app.main:create_app --factory --port ${APP_PORT:-8100}
    """
    settings = settings or get_settings()
    setup_logging(settings.app_log_level)
    validate_runtime_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """应用生命周期管理：启动时初始化依赖容器，关闭时清理资源。"""
        app.state.container = container if container is not None else Container(settings)
        if not app.state.container._started:
            await app.state.container.startup()
        logger.info("pet-consult 启动（env=%s mock=%s）", settings.app_env, settings.mock_mode)
        try:
            yield
        finally:
            await app.state.container.shutdown()

    app = FastAPI(
        title="Pet Consult API",
        version="1.0.0",
        description="宠物问诊助手（v5）。仅提供症状信息整理、风险分级与就医建议，不替代执业兽医诊断。",
        lifespan=lifespan,
    )
    # 注册中间件：请求 ID 注入、耗时记录、日志串联
    app.add_middleware(RequestContextMiddleware)
    # 注册异常处理器：统一 JSON 错误响应，不泄露内部信息
    install_error_handlers(app)
    # 注册路由：健康检查、问诊、会话管理、管理接口
    app.include_router(api_router)
    return app