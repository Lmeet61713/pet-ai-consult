"""图片安全校验（v5 §11.2 / V1.1 P0-6）

- 禁止只根据扩展名判断类型（魔数识别）；
- Pillow 最大像素数限制（防解压炸弹）：open 后先查像素数再 load，避免炸弹先吃内存；
- 拒绝异常压缩比图片（解压后尺寸/文件大小比）；
- 动画图片只取第一帧并记录日志；
- 只允许 JPEG/PNG/WEBP，单张 ≤5MB。

失败均抛 ImageRequestValidationError（请求/安全校验失败 → 400），
与"内容质量不可用"（Vision 判定）和"Vision 服务失败"严格区分。
"""
from __future__ import annotations

import io
import logging

from PIL import Image, UnidentifiedImageError

from app.core.config import Settings
from app.core.exceptions import ImageRequestValidationError, ImageTooLargeError

logger = logging.getLogger(__name__)

# 魔数 → 格式（真实格式识别，不信任扩展名）
_MAGIC: dict[bytes, str] = {
    b"\xff\xd8\xff": "JPEG",
    b"\x89PNG\r\n\x1a\n": "PNG",
    b"RIFF": "WEBP",  # 需二次确认 WEBP 头
}
_WEBP_HEADERS = (b"WEBPVP8 ", b"WEBPVP8X", b"WEBPVP8L")

# 解压后尺寸 / 原始字节 上限（异常压缩比拒绝）
MAX_EXPANSION_RATIO = 300


def sniff_format(data: bytes) -> str:
    """魔数识别真实格式；不认识抛 ImageRequestValidationError。"""
    for magic, fmt in _MAGIC.items():
        if data.startswith(magic):
            if fmt == "WEBP" and not any(data[8:16] == h for h in _WEBP_HEADERS):
                break
            return fmt
    raise ImageRequestValidationError("不支持的文件类型，仅支持 JPEG/PNG/WEBP")


def validate_bytes(data: bytes, settings: Settings) -> tuple[str, int, int]:
    """校验大小 + 魔数 + 像素数，返回 (format, width, height)。

    抛 ImageTooLargeError / ImageRequestValidationError。
    """
    if len(data) > settings.max_image_bytes:
        raise ImageTooLargeError(f"单张图片不能超过 {settings.max_image_bytes // (1024 * 1024)}MB")
    fmt = sniff_format(data)
    try:
        img = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ImageRequestValidationError("图片内容无法解码") from exc

    # 先查像素数再 load（V1.1 P0-6：解压炸弹先吃内存的问题）
    width, height = img.size
    pixels = width * height
    if pixels > settings.max_image_pixels:
        raise ImageRequestValidationError(
            f"图片像素过大（{width}x{height}），超过上限 {settings.max_image_pixels}"
        )

    try:
        img.load()  # 真实解码，验证完整
    except (OSError, SyntaxError) as exc:
        raise ImageRequestValidationError("图片内容无法解码") from exc

    if len(data) > 0 and pixels / len(data) > MAX_EXPANSION_RATIO:
        raise ImageRequestValidationError("图片压缩比异常，已拒绝")

    if getattr(img, "is_animated", False):
        # 明确记录并只取第一帧（V1.1 P2-5）
        logger.info("animated_image_first_frame", extra={"format": fmt})
        img.seek(0)
    return fmt, width, height


def reject_animated(data: bytes) -> bool:
    try:
        img = Image.open(io.BytesIO(data))
        return bool(getattr(img, "is_animated", False))
    except Exception:  # noqa: BLE001
        return False
