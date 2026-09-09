"""
图片预处理流水线（v5 §11.1）

接收 UploadFile 字节 → 校验 → 自动纠正方向 → 去 EXIF → 转 RGB →
长边压缩到 MAX_IMAGE_EDGE → 质量压缩 → SHA-256 → ProcessedImage。

安全设计：
- 解码前先校验格式/魔数/像素，防止解压炸弹攻击
- 去 EXIF 去除隐私元数据（GPS、相机型号等）
- 重新编码，清除所有 PNG 文本块、ICC 配置等元数据
- 失败抛 ImageRequestValidationError（400），与"Vision 服务不可用"严格区分
"""
from __future__ import annotations

import io
import logging

from PIL import Image, ImageOps

from app.core.config import Settings
from app.core.exceptions import ImageRequestValidationError
from app.image.validator import validate_bytes
from app.schemas.image import ProcessedImage
from app.utils.hashing import sha256_hex

logger = logging.getLogger(__name__)


def process_image(
    raw: bytes,
    *,
    image_id: str,
    filename: str,
    settings: Settings,
) -> ProcessedImage:
    """完整图片预处理流水线（内存模式）。

    处理步骤：
    1. validate_bytes：校验格式、魔数、像素数（防解压炸弹）
    2. Image.open + load：解码为 Pillow Image
    3. exif_transpose：自动纠正 EXIF orientation
    4. 长边压缩：按 MAX_IMAGE_EDGE 等比缩放
    5. 像素重建：去除所有元数据（EXIF/ICC/PNG text chunks）
    6. 重新编码：JPEG quality=85，优化存储
    7. SHA-256 指纹：用于缓存和内容去重

    Args:
        raw: 原始图片字节
        image_id: 图片唯一标识
        filename: 原始文件名（日志用）
        settings: 应用配置（含图片限制参数）

    Returns:
        预处理完成的 ProcessedImage 对象

    Raises:
        ImageRequestValidationError: 格式/解码/像素校验失败（400）
    """
    fmt, width, height = validate_bytes(raw, settings)
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise ImageRequestValidationError("图片内容无法解码") from exc

    # 自动纠正方向（EXIF orientation）
    img = ImageOps.exif_transpose(img)

    # 长边压缩到 MAX_IMAGE_EDGE（v5 §11.1）
    longest = max(img.size)
    if longest > settings.max_image_edge:
        ratio = settings.max_image_edge / longest
        img = img.resize(
            (int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS
        )

    # 像素重建：去除所有元数据。新 Pillow 图片不包含 EXIF、ICC、XMP、
    # PNG 文本块或其他 info 字典中的源元数据。
    if fmt == "JPEG":
        pixel_mode = "L" if img.mode == "L" else "RGB"
    else:
        has_alpha = "A" in img.getbands() or "transparency" in img.info
        pixel_mode = "RGBA" if has_alpha else "RGB"
    pixels = img.convert(pixel_mode)
    clean = Image.new(pixel_mode, pixels.size)
    clean.paste(pixels)
    img = clean

    buf = io.BytesIO()
    save_kwargs: dict = {}
    if fmt == "JPEG":
        save_kwargs = {"quality": 85, "optimize": True}
    img.save(buf, format=fmt, **save_kwargs)
    processed = buf.getvalue()

    return ProcessedImage(
        image_id=image_id,
        filename=filename,
        format=fmt,
        data=processed,
        width=img.width,
        height=img.height,
        sha256=sha256_hex(processed),
    )