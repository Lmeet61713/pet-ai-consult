"""通用数据结构：错误响应（v5 §17）"""
from __future__ import annotations

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ErrorResponse(BaseModel):
    request_id: str
    status: str = "error"
    error: ErrorDetail
