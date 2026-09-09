"""共享 VisionGateway 客户端（问诊不得直连 vLLM :8101）

- 请求携带 scene/request_id/priority/timeout_seconds（拓扑 V1.3 §5）
- 记录 queue_wait_ms / inference_ms / total_ms（指标与日志）
- 超时 → VisionTimeout；JSON 失败重试一次 → VisionOutputInvalid
- 超时/OOM/JSON 失败均由 agent 捕获降级为文本问诊（v6.3 §4 步骤 8）
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time

import httpx

from app.core.config import Settings
from app.core.deadline import Deadline
from app.core.exceptions import (
    VisionOutputInvalid,
    VisionTimeout,
    VisionUnavailable,
)
from app.image.output_parser import VisionOutputParser
from app.prompts.registry import PromptRegistry
from app.prompts.vision_v1 import SPEC as VISION_SPEC
from app.schemas.image import ProcessedImage, VisionFinding

logger = logging.getLogger(__name__)

# 单图单次调用最少预算（低于此值说明阶段剩余不足，直接放弃整批图片）
MIN_VISION_CALL_SECONDS = 2.0

# mock 预设（v6.3：model_confidence 可空，仅日志/评测）
_MOCK_BASE = {
    "image_quality": "good",
    "species_guess": "cat",
    "body_parts": ["眼部"],
    "observations": ["右眼分泌物增多", "轻微红肿"],
    "red_flags": [],
    "model_confidence": 0.78,
    "needs_more_images": False,
    "missing_views": [],
    "suggested_questions": ["这种情况持续多久了？", "有没有抓挠或眯眼？"],
}


class VisionGatewayClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._http: httpx.AsyncClient | None = None
        self.registry = PromptRegistry()
        self.registry.register(VISION_SPEC)

    @property
    def is_mock(self) -> bool:
        return self.s.mock_vision

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.s.vision_gateway_base_url,
                timeout=httpx.Timeout(self.s.vision_timeout_seconds + 2.0, connect=3.0),
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def ping(self) -> bool:
        """健康检查（v6.3 §10.4 ready：检查 VisionGateway 而非 vLLM）。"""
        if self.is_mock:
            return True
        try:
            resp = await self._http_client().get("/health")
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def analyze(
        self,
        images: list[ProcessedImage],
        text_hint: str | None = None,
        *,
        deadline: Deadline | None = None,
        request_id: str = "",
        telemetry: list[dict] | None = None,
    ) -> list[VisionFinding]:
        """在同一阶段总预算内并发分析多图。

        单次请求最多三张图。客户端并发提交，VisionGateway 的全局信号量负责限制
        实际模型并发；所有图片共享同一个 wall-clock deadline。任一任务失败时取消
        尚未完成的同批任务，避免超时后继续占用视觉队列。
        """
        if self.is_mock:
            return self._mock_findings(images, text_hint)
        if not images:
            return []

        stage = deadline or Deadline.after_seconds(self.s.vision_timeout_seconds)
        call_cap = stage.require(minimum=MIN_VISION_CALL_SECONDS)
        tasks = [
            asyncio.create_task(
                self._analyze_one(
                    img,
                    text_hint,
                    stage,
                    call_cap,
                    request_id=request_id,
                    telemetry=telemetry,
                ),
                name=f"vision-{img.image_id}",
            )
            for img in images
        ]
        try:
            # asyncio.gather 按传入顺序返回，保持结果与上传图片顺序一致。
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    # ------------------------------------------------------------ 内部

    async def _analyze_one(
        self,
        img: ProcessedImage,
        text_hint: str | None,
        stage: Deadline,
        call_cap: float,
        *,
        request_id: str,
        telemetry: list[dict] | None,
    ) -> VisionFinding:
        # 队列化模式下图片经临时文件传递（data 为空）：回退读 temp_path
        image_bytes = img.data
        if not image_bytes and img.temp_path:
            from pathlib import Path as _Path

            image_bytes = _Path(img.temp_path).read_bytes()
        prompt = self.registry.get(VISION_SPEC.prompt_id).template.format(
            n_images=1, user_text=text_hint or "无"
        )
        payload = {
            "scene": self.s.vision_scene,
            "request_id": request_id or "req_" + img.image_id,
            "priority": self.s.vision_priority,
            "timeout_seconds": call_cap,
            "max_tokens": self.s.vision_max_tokens,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/{img.format.lower()};base64,"
                            + base64.b64encode(image_bytes).decode("ascii"),
                        },
                    },
                ]},
            ],
        }

        async def _call(timeout: float) -> tuple[dict, int, int, int, int]:
            t0 = time.perf_counter()
            try:
                resp = await self._http_client().post(
                    "/v1/vision", json=payload,
                    timeout=httpx.Timeout(timeout, connect=3.0),
                )
            except httpx.TimeoutException as exc:
                raise VisionTimeout("图片解析超时，已降级为文本问诊") from exc
            except httpx.HTTPError as exc:
                raise VisionUnavailable(f"VisionGateway 不可用: {exc}") from exc
            elapsed_ms = round((time.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                raise VisionUnavailable(f"VisionGateway 返回 {resp.status_code}")
            data = resp.json()
            return (
                data,
                elapsed_ms,
                int(data.get("queue_wait_ms", 0)),
                int(data.get("inference_ms", 0)),
                int(data.get("total_ms", elapsed_ms)),
            )

        call_started = time.perf_counter()
        queue_ms = infer_ms = gateway_ms = 0
        try:
            data, client_ms, queue_ms, infer_ms, gateway_ms = await _call(call_cap)
            logger.info(
                "vision_done",
                extra={"image_id": img.image_id, "total_ms": client_ms,
                       "queue_wait_ms": queue_ms, "inference_ms": infer_ms},
            )
            finding = VisionOutputParser.parse(data.get("content", ""), image_id=img.image_id)
            if telemetry is not None:
                telemetry.append(
                    {
                        "image_id": img.image_id,
                        "cache_hit": False,
                        "queue_wait_ms": queue_ms,
                        "inference_ms": infer_ms,
                        "gateway_total_ms": gateway_ms,
                        "client_total_ms": client_ms,
                        "format_retries": 0,
                    }
                )
            return finding
        except (VisionTimeout, VisionUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - 格式失败重试一次（只拿剩余预算）
            logger.warning("图片 %s 解析格式失败（重试一次）: %s", img.image_id, exc)
            try:
                retry_cap = stage.require(minimum=1.0)
                retry_data, retry_client_ms, retry_queue_ms, retry_infer_ms, retry_gateway_ms = (
                    await _call(retry_cap)
                )
                finding = VisionOutputParser.parse(
                    retry_data.get("content", ""), image_id=img.image_id
                )
                if telemetry is not None:
                    telemetry.append(
                        {
                            "image_id": img.image_id,
                            "cache_hit": False,
                            "queue_wait_ms": queue_ms + retry_queue_ms,
                            "inference_ms": infer_ms + retry_infer_ms,
                            "gateway_total_ms": gateway_ms + retry_gateway_ms,
                            "client_total_ms": round(
                                (time.perf_counter() - call_started) * 1000
                            ),
                            "format_retries": 1,
                            "retry_client_ms": retry_client_ms,
                        }
                    )
                return finding
            except Exception as exc2:  # noqa: BLE001
                logger.error("图片 %s 两次均失败: %s", img.image_id, exc2)
                raise VisionOutputInvalid("图片解析输出连续失败") from exc2

    @staticmethod
    def _mock_findings(
        images: list[ProcessedImage], text_hint: str | None
    ) -> list[VisionFinding]:
        findings: list[VisionFinding] = []
        for i, img in enumerate(images):
            data = dict(_MOCK_BASE)
            if len(images) > 1:
                data["observations"] = [f"图{i + 1}: {o}" for o in _MOCK_BASE["observations"]]
                data["model_confidence"] = min(0.95, 0.78 + 0.05 * i)
            finding = VisionFinding(image_id=img.image_id, **data)
            if not (text_hint or "").strip():
                finding.suggested_questions.append("请补充描述：症状持续多久了？精神状态如何？")
            findings.append(finding)
        return findings
