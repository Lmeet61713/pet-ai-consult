"""客户端抽象（v6.3 §8 Protocol）与公共基类

业务层只依赖抽象；mock 与真实实现可替换。
"""
from __future__ import annotations

from typing import Protocol

from app.schemas.consult import GeneratedConsultation, KnowledgeConsultRequest
from app.schemas.image import ProcessedImage, VisionFinding
from app.schemas.safety import ModerationResult


class VisionModelClient(Protocol):
    """共享 VisionGateway：图片 → 结构化观察列表"""

    async def analyze(
        self,
        images: list[ProcessedImage],
        text_hint: str | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> list[VisionFinding]: ...

    async def close(self) -> None: ...


class KnowledgeConsultClient(Protocol):
    """已验证的外部知识问诊 API（v6.3 §14.3：业务只依赖此抽象）"""

    async def generate_consultation(
        self,
        *,
        request: KnowledgeConsultRequest,
        timeout_seconds: float,
        request_id: str,
    ) -> GeneratedConsultation: ...

    async def close(self) -> None: ...


class ModerationClient(Protocol):
    """Qwen3Guard GPU 服务（场景化审核）"""

    async def check(
        self,
        content: str,
        *,
        scene: str,
        timeout_seconds: float,
    ) -> ModerationResult: ...

    async def close(self) -> None: ...
