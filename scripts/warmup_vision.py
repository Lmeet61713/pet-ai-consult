"""Warm the VisionGateway with the production request shape before API startup."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.clients.vision_gateway_client import VisionGatewayClient  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.deadline import Deadline  # noqa: E402
from app.schemas.image import ProcessedImage  # noqa: E402


WARMUP_WIDTH = 855
WARMUP_HEIGHT = 663


def _processed_image(raw: bytes, *, filename: str, image_format: str) -> ProcessedImage:
    with Image.open(BytesIO(raw)) as image:
        width, height = image.size
        image.verify()
    return ProcessedImage(
        image_id="vision_warmup",
        filename=filename,
        format=image_format,
        data=raw,
        width=width,
        height=height,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def build_warmup_image(path: str | None = None) -> ProcessedImage:
    """Load an optional representative image, or generate one with a stable shape."""
    if path:
        image_path = Path(path)
        raw = image_path.read_bytes()
        with Image.open(BytesIO(raw)) as image:
            image_format = (image.format or "").upper()
        if image_format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError(f"Unsupported warmup image format: {image_path}")
        return _processed_image(raw, filename=image_path.name, image_format=image_format)

    buffer = BytesIO()
    Image.new("RGB", (WARMUP_WIDTH, WARMUP_HEIGHT), (160, 160, 160)).save(buffer, "PNG")
    return _processed_image(buffer.getvalue(), filename="vision-warmup.png", image_format="PNG")


async def run_warmup(*, image_path: str | None, timeout_seconds: float) -> None:
    settings = Settings(
        app_env="test",
        mock_mode=False,
        mock_vision=False,
        vision_timeout_seconds=timeout_seconds,
    )
    client = VisionGatewayClient(settings)
    try:
        findings = await client.analyze(
            [build_warmup_image(image_path)],
            text_hint="启动预热，请只返回简短的可见事实。",
            deadline=Deadline.after_seconds(timeout_seconds),
            request_id="vision_warmup",
        )
        if len(findings) != 1:
            raise RuntimeError(f"Vision warmup returned {len(findings)} findings")
        print(
            "Vision warmup complete: "
            f"quality={findings[0].image_quality.value} "
            f"image_id={findings[0].image_id}"
        )
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=os.getenv("VISION_WARMUP_IMAGE"))
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("VISION_WARMUP_TIMEOUT_SECONDS", "60")),
    )
    args = parser.parse_args()
    asyncio.run(run_warmup(image_path=args.image, timeout_seconds=args.timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
