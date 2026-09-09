"""FastAPI 异常转换（v5 §17：统一错误响应，不泄露内部信息）"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError as FastAPIRequestValidationError
from fastapi.responses import JSONResponse

from app.core.exceptions import PetConsultError

logger = logging.getLogger(__name__)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(FastAPIRequestValidationError)
    async def request_validation_handler(
        request: Request, exc: FastAPIRequestValidationError
    ):
        request_id = getattr(request.state, "request_id", "")
        logger.warning(
            "request_validation_error",
            extra={"request_id": request_id, "error_count": len(exc.errors())},
        )
        return JSONResponse(
            status_code=400,
            content={
                "request_id": request_id,
                "status": "error",
                "error": {
                    "code": "BAD_REQUEST",
                    "message": "请求参数格式不正确",
                    "retryable": False,
                },
            },
        )

    @app.exception_handler(PetConsultError)
    async def pet_consult_handler(request: Request, exc: PetConsultError):
        request_id = getattr(request.state, "request_id", "")
        logger.warning(
            "business_error",
            extra={"request_id": request_id, "code": exc.code, "retryable": exc.retryable},
        )
        headers = {"Retry-After": "2"} if exc.code == "QUEUE_BUSY" else None
        return JSONResponse(
            status_code=exc.http_status,
            headers=headers,
            content={
                "request_id": request_id,
                "status": "error",
                "error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable},
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "")
        logger.exception(
            "unhandled_error",
            exc_info=exc,
            extra={"request_id": request_id},
        )
        return JSONResponse(
            status_code=500,
            content={
                "request_id": request_id,
                "status": "error",
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务内部错误，请稍后重试。",
                    "retryable": True,
                },
            },
        )
