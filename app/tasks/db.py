"""队列化数据库会话（Phase 2 §4.4：consult_task / consult_outbox / consult_task_event）。

复用 pet-moderation 的 SQLAlchemy 2.0 asyncio 模式；consult_database_url 为空时
队列化关闭（Phase 1 直连模式）。
"""
from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def build_engine(
    database_url: str,
    password: str = "",
    *,
    pool_size: int = 20,
    max_overflow: int = 10,
    pool_timeout: float = 10.0,
) -> AsyncEngine:
    """密码含 @ 等特殊字符时 URL 解析会错（SQLAlchemy 按第一个 @ 切分）：
    密码由调用方从独立环境变量传入，这里 set 回 URL。"""
    if password:
        url = make_url(database_url).set(password=password)
    else:
        url = make_url(database_url)
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
