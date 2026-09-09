"""Token 校验（V1.1 P0-1：只验不签，tenant_id / user_id 只从验证后的 JWT 获取）

- JWT 独立配置（JWT_SIGNING_SECRET 不复用 APP_SECRET_KEY），校验 issuer / audience / scope / exp
- 本服务不签发 Token；签发归业务认证服务
- mock 模式：跳过 JWT，但要求 X-User-Id 请求头做用户隔离
- production 环境禁止 mock_mode
- auth_skip=True：同样跳过 JWT（改用 X-User-Id），但不引入 mock 的 fakeredis 副作用
"""
from __future__ import annotations

import jwt
from fastapi import Depends, Header

from app.core.config import Settings
from app.core.constants import DEFAULT_TENANT
from app.core.dependencies import get_app_settings
from app.core.exceptions import RequestValidationError, UnauthorizedError
from app.schemas.auth import AuthContext


def get_auth_context(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    settings: Settings = Depends(get_app_settings),
) -> AuthContext:
    if settings.mock_mode or settings.auth_skip:
        if not x_user_id or not x_user_id.strip():
            raise RequestValidationError("缺少 X-User-Id 请求头，请传入用户标识")
        return AuthContext(
            tenant_id=DEFAULT_TENANT,
            user_id=x_user_id.strip(),
            token_valid=False,
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError("无效或缺失 Token")

    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_signing_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["sub", "tenant_id", "exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("无效或过期 Token") from exc

    raw_scope = payload.get("scope", [])
    # OAuth/JWT 生态同时存在 list 和空格分隔字符串两种 scope 表达。
    scopes = set(raw_scope.split()) if isinstance(raw_scope, str) else set(raw_scope)
    if settings.jwt_required_scope not in scopes:
        raise UnauthorizedError("Token 无问诊权限")

    return AuthContext(
        tenant_id=str(payload["tenant_id"]),
        user_id=str(payload["sub"]),
        token_valid=True,
        scope=sorted(scopes),
    )
