"""并发压测（v5 §25 / M7：单图、三图 P95 与并发）

用法：python scripts/benchmark.py --base http://127.0.0.1:18100 --concurrency 4 --n 20 [--image pet.jpg] [--token xxx]
统计：成功率、状态占比、P50/P95/P99、总耗时。

注意（V1.1 P0-5）：每个请求使用独立 conversation_id（uuid），
否则同会话串行锁会把压测退化为串行、P95 失真。
"""
from __future__ import annotations

import argparse
import asyncio
import math
import mimetypes
import sys
import time
import uuid
from pathlib import Path

import httpx


def media_type_for(path: str) -> str:
    """Return a supported multipart media type for the benchmark image."""
    suffix = Path(path).suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix) or mimetypes.guess_type(path)[0]
    if media_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("--image 仅支持 JPEG、PNG、WEBP")
    return media_type


async def one(
    client: httpx.AsyncClient,
    text: str,
    image: bytes | None,
    image_name: str | None,
    image_media_type: str | None,
    token: str | None,
    results: list,
) -> None:
    form = {"conversation_id": f"bench_{uuid.uuid4().hex[:12]}", "text": text}
    files = (
        [("images", (image_name or "pet.jpg", image, image_media_type or "image/jpeg"))]
        if image
        else None
    )
    headers = {"Authorization": f"Bearer {token}"} if token else None
    t0 = time.perf_counter()
    try:
        r = await client.post("/api/v1/consult", data=form, files=files, headers=headers)
        results.append({"ms": (time.perf_counter() - t0) * 1000, "status": r.status_code, "body": r.json().get("status")})
    except Exception as exc:  # noqa: BLE001
        results.append({"ms": (time.perf_counter() - t0) * 1000, "status": 0, "body": str(exc)})


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18100")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--image", default=None)
    parser.add_argument("--token", default=None, help="生产环境 JWT（可选）")
    args = parser.parse_args()

    image = open(args.image, "rb").read() if args.image else None
    image_media_type = media_type_for(args.image) if args.image else None
    image_name = Path(args.image).name if args.image else None
    text = "眼睛分泌物多，三天了，精神还行"

    results: list = []
    async with httpx.AsyncClient(
        base_url=args.base, timeout=60, trust_env=False
    ) as client:
        for batch in range(0, args.n, args.concurrency):
            await asyncio.gather(
                *[
                    one(client, text, image, image_name, image_media_type, args.token, results)
                    for _ in range(min(args.concurrency, args.n - batch))
                ]
            )

    ok = [r for r in results if r["status"] == 200]
    ms = sorted(r["ms"] for r in ok)
    statuses = {}
    for r in ok:
        statuses[r["body"]] = statuses.get(r["body"], 0) + 1

    def pct(p: float) -> float:
        return ms[max(0, math.ceil(len(ms) * p) - 1)] if ms else 0.0

    print(f"总请求 {len(results)}，成功 {len(ok)}，失败 {len(results) - len(ok)}")
    print(f"状态分布: {statuses}")
    if ms:
        print(f"P50={pct(0.5):.0f}ms P95={pct(0.95):.0f}ms P99={pct(0.99):.0f}ms")
    return 0 if len(ok) == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
