"""图片元数据与视觉结果（v6.3 §7.3）"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.constants import ImageQuality


class ProcessedImage(BaseModel):
    """图片处理流水线输出（内存或请求级临时文件）"""

    image_id: str
    filename: str
    format: str                 # JPEG | PNG | WEBP
    data: bytes | None = None       # 图片在内存中的数据
    temp_path: str | None = None       # 临时文件路径，存放比较大的图像数据，避免反复将大文件在内存中加载
    width: int = 0
    height: int = 0
    sha256: str = ""       # 图片SHA256哈希值


class VisionFinding(BaseModel):
    """单张图片的结构化观察（v6.3：model_confidence 可空，仅日志/评测用）"""

    image_id: str
    image_quality: ImageQuality       # 图片质量等级
    species_guess: str = "unknown"       # 图片物种猜测
    body_parts: list[str] = Field(default_factory=list)       # 图片身体部位猜测
    observations: list[str] = Field(default_factory=list)       # 图片观察结果
    red_flags: list[str] = Field(default_factory=list)       # 图片红色标志
    model_confidence: float | None = Field(default=None, ge=0, le=1)       # 模型置信度
    needs_more_images: bool = False       # 是否需要更多图片
    missing_views: list[str] = Field(default_factory=list)       # 缺失的视角
    suggested_questions: list[str] = Field(default_factory=list)       # 建议的问题
