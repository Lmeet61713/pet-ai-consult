"""AutoDL 真实链路验收：配置、契约、依赖健康和问诊冒烟。"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from app.core.config import Settings

# Direct execution sets sys.path[0] to scripts/, so make the documented
# `python scripts/autodl_acceptance.py` command resolve the app package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def media_type_for(path: Path) -> str:
    """Return the multipart media type matching the real supported image suffix."""
    try:
        return _MEDIA_TYPES[path.suffix.lower()]
    except KeyError as exc:
        raise ValueError("仅支持 JPEG、PNG、WEBP 验收图片") from exc


class Results:
    def __init__(self) -> None:
        self.failed = 0

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.failed += int(not ok)
        suffix = f" - {detail}" if detail else ""
        print(f"{'PASS' if ok else 'FAIL'} {name}{suffix}")


async def check_tcp(results: Results, name: str, host: str, port: int) -> None:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
        writer.close()
        await writer.wait_closed()
        results.record(name, True, f"{host}:{port}")
    except Exception as exc:  # noqa: BLE001
        results.record(name, False, f"{host}:{port} {type(exc).__name__}")


async def check_health(results: Results, name: str, url: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(url)
        data = response.json() if response.content else {}
        results.record(name, response.status_code == 200, f"HTTP {response.status_code}")
        return data
    except Exception as exc:  # noqa: BLE001
        results.record(name, False, type(exc).__name__)
        return None


def validate_settings(results: Results, settings: Settings) -> None:
    from app.core.contract_store import ContractStore

    results.record("APP_ENV=test", settings.app_env == "test", settings.app_env)
    mocks = {
        "MOCK_MODE": settings.mock_mode,
        "MOCK_VISION": settings.mock_vision,
        "MOCK_KNOWLEDGE_CONSULT": settings.mock_knowledge_consult,
        "MOCK_GUARD": settings.mock_guard,
    }
    for name, enabled in mocks.items():
        results.record(f"{name}=false", not enabled)
    results.record("GUARD_MODE 合法", settings.guard_mode in {"off", "shadow", "enforce"}, settings.guard_mode)
    results.record("JWT_SIGNING_SECRET 已配置", bool(settings.jwt_signing_secret))
    results.record("LOG_HASH_SECRET 已配置", bool(settings.log_hash_secret))
    results.record("REDIS_PASSWORD 已配置", bool(settings.redis_password))
    results.record("Knowledge API Key 已配置", bool(settings.knowledge_api_key))
    results.record(
        "DeepSeek 契约标记有效",
        ContractStore(settings).load_verified_contract() is not None,
    )


async def run_consult(
    results: Results,
    *,
    base_url: str,
    token: str,
    image_path: Path,
    text: str,
) -> None:
    data = {
        "conversation_id": f"autodl_{uuid.uuid4().hex[:12]}",
        "text": text,
    }
    files = {
        "images": (
            image_path.name,
            image_path.read_bytes(),
            media_type_for(image_path),
        )
    }
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=60.0, trust_env=False) as client:
            response = await client.post(
                "/api/v1/consult",
                data=data,
                files=files,
                headers={"Authorization": f"Bearer {token}"},
            )
        body = response.json()
        ok = (
            response.status_code == 200
            and body.get("status") == "success"
            and bool(body.get("answer"))
            and bool(body.get("disclaimer"))
            and body.get("risk_level") is not None
            and body.get("knowledge_degraded") is False
            and "redis" not in body.get("risk_flags", [])
            and "vision" not in body.get("risk_flags", [])
        )
        results.record(
            "真实问诊冒烟",
            ok,
            f"HTTP {response.status_code} status={body.get('status')} "
            f"mode={body.get('answer_mode')}",
        )
    except Exception as exc:  # noqa: BLE001
        results.record("真实问诊冒烟", False, type(exc).__name__)


async def main() -> int:
    from app.core.config import Settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:18100", help="宿主机 API 地址")
    parser.add_argument(
        "--token",
        default=os.getenv("AUTODL_TEST_JWT", ""),
        help="测试 JWT；也可使用 AUTODL_TEST_JWT",
    )
    parser.add_argument("--image", type=Path, required=True, help="真实宠物测试图片")
    parser.add_argument(
        "--text",
        default="宠物眼睛分泌物增多三天，精神和食欲正常，需要注意什么？",
        help="与测试图片匹配的问诊文本",
    )
    args = parser.parse_args()

    results = Results()
    settings = Settings()
    validate_settings(results, settings)

    redis = urlparse(settings.redis_url)
    vision = urlparse(settings.vision_gateway_base_url)
    guard = urlparse(settings.guard_base_url)
    await check_tcp(results, "Redis 端口", redis.hostname or "127.0.0.1", redis.port or 6379)
    await check_tcp(results, "VisionGateway 端口", vision.hostname or "127.0.0.1", vision.port or 8102)
    if settings.guard_enforced:
        await check_tcp(results, "Guard 端口", guard.hostname or "127.0.0.1", guard.port or 8103)

    await check_health(results, "VisionGateway /health", settings.vision_gateway_base_url + "/health")
    if settings.guard_enforced:
        await check_health(results, "Guard /health", settings.guard_base_url + "/health")
    await check_health(results, "API /health/live", args.base.rstrip("/") + "/health/live")
    ready = await check_health(
        results, "API /health/ready", args.base.rstrip("/") + "/health/ready"
    )
    if ready is not None:
        checks = ready.get("checks", {})
        results.record("API 依赖全部 ready", bool(checks) and all(checks.values()), str(checks))

    if not args.token:
        results.record("真实 JWT Token 已提供", False, "使用 --token 或 AUTODL_TEST_JWT")
    elif not args.image.is_file():
        results.record("测试图片存在", False, str(args.image))
    else:
        await run_consult(
            results,
            base_url=args.base,
            token=args.token,
            image_path=args.image,
            text=args.text,
        )

    print(f"\n结果：{'PASS' if results.failed == 0 else 'FAIL'}，失败项 {results.failed}")
    return 1 if results.failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
