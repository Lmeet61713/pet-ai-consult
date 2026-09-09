"""Qwen3Guard GPU 服务客户端（拓扑 V1.3 §6 / v6.3 §13.1.1）

Guard 服务负责：短文本排队、GPU 并发限制、max_new_tokens=32、模型纯文本解析、
真实标签 mapping、按 scene 返回标准业务结果（JSON）。

客户端契约：POST /v1/moderate {scene, text, request_id} → {blocked, verdict, categories, parse_ok}
场景化：pet_consult_input / pet_consult_output；医疗求助中的伤口/出血/车祸描述
不因 Violent 标签直接拒绝（should_refuse_medical_request 由规则层判定）。
调用失败或 JSON 无法解析 → 保守 Review（不静默放行，v6.3 §28）。
"""
from __future__ import annotations

import logging

import httpx

from app.core.config import Settings
from app.core.constants import GUARD_SCENE_INPUT
from app.schemas.safety import ModerationResult
from app.utils.json_parser import extract_json_object

logger = logging.getLogger(__name__)


class GuardClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._http: httpx.AsyncClient | None = None

    @property
    def is_mock(self) -> bool:
        return self.s.mock_guard

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.s.guard_base_url,
                timeout=httpx.Timeout(3.0, connect=1.0),
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def ping(self) -> bool:
        if self.is_mock:
            return True
        try:
            resp = await self._http_client().get(
                "/health", timeout=httpx.Timeout(2.0, connect=1.0)
            )
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def check(
        self,
        content: str,
        *,
        scene: str = GUARD_SCENE_INPUT,
        timeout_seconds: float | None = None,
        request_id: str = "",
    ) -> ModerationResult:
        if not content.strip():
            return ModerationResult(verdict="Safe", parse_ok=True, scene=scene)
        if self.is_mock:
            return ModerationResult(verdict="Safe", parse_ok=True, scene=scene)

        budget = timeout_seconds or (
            self.s.guard_input_timeout if scene == GUARD_SCENE_INPUT else self.s.guard_output_timeout
        )
        body = {"scene": scene, "text": content[:2000], "request_id": request_id or "guard"}
        try:
            resp = await self._http_client().post(
                "/v1/moderate", json=body,
                timeout=httpx.Timeout(budget, connect=1.0),
            )
            resp.raise_for_status()
            obj = extract_json_object(resp.text)
            return ModerationResult(
                blocked=bool(obj.get("blocked", False)),
                verdict=obj.get("verdict", "Review"),
                categories=obj.get("categories", []),
                raw=resp.text,
                parse_ok=bool(obj.get("parse_ok", True)),
                scene=scene,
            )
        except Exception as exc:  # noqa: BLE001 - 失败保守 Review
            logger.warning("Guard 调用失败（scene=%s）: %s", scene, exc)
            return ModerationResult(blocked=True, verdict="Review", parse_ok=False, scene=scene)
