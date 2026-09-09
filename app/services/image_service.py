"""图片服务：经 VisionGateway 解析 + 摘要生成（v6.3 §4 步骤 6-9）"""
from __future__ import annotations

import hashlib
import logging
import re

from app.clients.vision_gateway_client import VisionGatewayClient
from app.core.config import Settings
from app.core.deadline import Deadline
from app.schemas.image import ProcessedImage, VisionFinding
from app.prompts.vision_v1 import SPEC as VISION_SPEC

logger = logging.getLogger(__name__)


class ImageService:
    def __init__(
        self,
        settings: Settings,
        vision_client: VisionGatewayClient,
        *,
        cache_client=None,
    ):
        self.s = settings
        self.vision = vision_client
        self.cache = cache_client

    async def analyze(
        self,
        images: list[ProcessedImage],
        text_hint: str | None = None,
        *,
        deadline: Deadline | None = None,
        request_id: str = "",
        telemetry: list[dict] | None = None,
    ) -> list[VisionFinding]:
        """调用共享 VisionGateway（V1.1 P0-3：多图共享阶段预算）。

        超时/OOM/JSON 失败由异常向上抛（agent 降级）。
        """
        if (
            not self.s.vision_cache_enabled
            or self.cache is None
            or self.vision.is_mock
        ):
            return await self.vision.analyze(
                images,
                text_hint,
                deadline=deadline,
                request_id=request_id,
                telemetry=telemetry,
            )

        results: dict[str, VisionFinding] = {}
        missing: list[ProcessedImage] = []
        keys: dict[str, str] = {}
        for image in images:
            key = self._cache_key(image, text_hint)
            keys[image.image_id] = key
            try:
                cached = await self.cache.get(key)
                if cached:
                    results[image.image_id] = VisionFinding.model_validate_json(cached).model_copy(
                        update={"image_id": image.image_id}
                    )
                    if telemetry is not None:
                        telemetry.append(
                            {
                                "image_id": image.image_id,
                                "cache_hit": True,
                                "queue_wait_ms": 0,
                                "inference_ms": 0,
                                "gateway_total_ms": 0,
                                "format_retries": 0,
                            }
                        )
                    continue
            except Exception:  # noqa: BLE001 - 缓存失败回退实时视觉
                logger.warning("vision_cache_read_failed", exc_info=True)
            missing.append(image)

        if missing:
            findings = await self.vision.analyze(
                missing,
                text_hint,
                deadline=deadline,
                request_id=request_id,
                telemetry=telemetry,
            )
            for finding in findings:
                results[finding.image_id] = finding
                try:
                    await self.cache.set(
                        keys[finding.image_id],
                        finding.model_dump_json(),
                        ex=self.s.vision_cache_ttl_seconds,
                    )
                except Exception:  # noqa: BLE001 - 缓存写失败不影响本次结果
                    logger.warning("vision_cache_write_failed", exc_info=True)

        # 保持与上传图片一致的顺序。
        return [results[image.image_id] for image in images]

    def _cache_key(self, image: ProcessedImage, text_hint: str | None) -> str:
        normalized_hint = re.sub(r"\s+", " ", (text_hint or "").strip().lower())
        hint_hash = hashlib.sha256(normalized_hint.encode("utf-8")).hexdigest()
        material = "|".join(
            (
                image.sha256,
                self.s.consult_vision_model_name,
                VISION_SPEC.version,
                f"edge={self.s.max_image_edge}",
                f"tokens={self.s.vision_max_tokens}",
                hint_hash,
            )
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"{self.s.redis_namespace}:vision-cache:{digest}"

    @staticmethod
    def build_summary(findings: list[VisionFinding]) -> dict:
        """合并成给 KnowledgeConsult 的 image_summary（v6.3 §14.2）。"""
        observations = [o for f in findings for o in f.observations]
        red_flags = [f for f in findings for f in f.red_flags]
        limitations = [
            f"图片质量 {f.image_quality.value}" for f in findings if f.image_quality.value != "good"
        ]
        return {
            "observations": observations,
            "red_flags": red_flags,
            "limitations": limitations,
        }

    async def close(self) -> None:
        await self.vision.close()
