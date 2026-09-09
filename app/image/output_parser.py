"""视觉输出解析与规范化（v5 §12.4）

模型原始输出 → JSON 提取 → Pydantic 校验 → 规范化（未知字段兜底）。
失败抛 ModelOutputValidationError（上层重试一次，仍失败 need_more_info/review）。
"""
from __future__ import annotations

import logging

from pydantic import ValidationError

from app.core.constants import ImageQuality
from app.core.exceptions import ModelOutputValidationError
from app.schemas.image import VisionFinding
from app.utils.json_parser import extract_json_object

logger = logging.getLogger(__name__)


class VisionOutputParser:
    """把单图模型输出解析为 VisionFinding。"""

    @staticmethod
    def parse(raw: str, *, image_id: str) -> VisionFinding:
        # image_id is a request-side correlation value, never model output.
        # Insert it before validation because VisionFinding requires it.
        data = extract_json_object(raw)
        data["image_id"] = image_id
        try:
            return VisionFinding.model_validate(data)
        except ValidationError as exc:
            raise ModelOutputValidationError(f"模型 JSON 字段校验失败: {exc}") from exc

    @staticmethod
    def unusable(image_id: str, reason: str = "") -> VisionFinding:
        """解析失败兜底（v5 §13.4：图片无法判断 → need_more_info）"""
        return VisionFinding(
            image_id=image_id,
            image_quality=ImageQuality.UNUSABLE,
        )
