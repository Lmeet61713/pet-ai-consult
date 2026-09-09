"""汇总所有 API Router（v5 §5 api/router.py）"""
from __future__ import annotations

from fastapi import APIRouter

from app.api import admin, consult, conversations, health

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(consult.router)
api_router.include_router(conversations.router)
api_router.include_router(admin.router)
