"""共享 VisionGateway 冒烟（v6.3 §24.2：走 Gateway，不直连 vLLM；解析成功率 100%）

用法：python scripts/smoke_vision_gateway.py <图片1> [图片2] [图片3]
前置：问诊模型服务已启动（vLLM :8101 + VisionGateway :8102）。
"""
from __future__ import annotations

import asyncio
import sys
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from app.schemas.image import ProcessedImage

# Direct execution sets sys.path[0] to scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_processed_image(path_value: str, index: int) -> ProcessedImage:
    from app.schemas.image import ProcessedImage

    path = Path(path_value)
    raw = path.read_bytes()
    with Image.open(BytesIO(raw)) as source:
        image_format = (source.format or "").upper()
        width, height = source.size
        source.verify()
    if image_format not in {"JPEG", "PNG", "WEBP"}:
        raise ValueError(f"不支持的图片格式: {path}")
    return ProcessedImage(
        image_id=f"img_{index}",
        filename=path.name,
        format=image_format,
        data=raw,
        width=width,
        height=height,
    )


async def main(paths: list[str]) -> int:
    from app.clients.vision_gateway_client import VisionGatewayClient
    from app.core.config import Settings

    settings = Settings(app_env="test", mock_mode=False, mock_vision=False)
    client = VisionGatewayClient(settings)
    images = [_load_processed_image(path, index) for index, path in enumerate(paths, start=1)]
    findings = await client.analyze(images, text_hint="冒烟测试")
    ok = 0
    for f in findings:
        print(
            f"图 {f.image_id}: quality={f.image_quality} species={f.species_guess} "
            f"parts={f.body_parts} obs={f.observations} red_flags={f.red_flags} "
            f"conf={f.model_confidence}"
        )
        if f.image_quality != "unusable":
            ok += 1
    await client.close()
    print(f"解析成功 {ok}/{len(findings)}")
    return 0 if ok == len(findings) else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1:])))
