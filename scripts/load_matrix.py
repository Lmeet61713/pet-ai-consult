"""v7.3 文本/图片阶梯压测：区分冷缓存、热缓存、禁用缓存与工作负载。

示例：
    python scripts/load_matrix.py --workload text --concurrency 6,10,14,20
    python scripts/load_matrix.py --workload single-image --image pet.jpg \
        --cache-mode cold --concurrency 1,2,4,6,8,12
    python scripts/load_matrix.py --workload triple-image --image a.jpg --image b.jpg \
        --image c.jpg --cache-mode cold --concurrency 1,2,3,4
    python scripts/load_matrix.py --workload mixed --image pet.jpg --image-percent 20 \
        --concurrency 20,24,32

每个请求始终使用独立 conversation_id、X-Request-Id 和 Idempotency-Key。
成功延迟只统计 HTTP 2xx 且业务 status 非 error 的请求，HTTP 503 不进入分位数。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import mimetypes
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

SUPPORTED_IMAGES = {"image/jpeg", "image/png", "image/webp"}
STAGE_EVENT_MAP = {
    "vision_completed": "vision_ms",
    "answer_generated": "generate_ms",
    "medical_review_completed": "medical_review_ms",
    "output_review_completed": "output_moderation_ms",
}


def media_type_for(path: str) -> str:
    suffix = Path(path).suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(suffix) or mimetypes.guess_type(path)[0]
    if media_type not in SUPPORTED_IMAGES:
        raise ValueError("--image 仅支持 JPEG、PNG、WEBP")
    return media_type


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * p) - 1)] if ordered else 0.0


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in [*text.splitlines(), ""]:
        line = raw_line.rstrip("\r")
        if not line:
            if data_lines:
                try:
                    payload = json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    payload = {"raw": "\n".join(data_lines)}
                events.append((event_name, payload))
            event_name = "message"
            data_lines = []
        elif line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
    return events


def build_request_text(base_text: str, cache_mode: str, request_id: str, has_images: bool) -> str:
    if has_images and cache_mode == "cold":
        # 仅改变缓存键；业务语义保持不变。服务端会规范化 text_hint 后参与缓存键。
        return f"{base_text} [视觉压测样本 {request_id}]"
    return base_text


def choose_images(
    workload: str,
    all_images: list[tuple[str, bytes, str]],
    request_index: int,
    image_percent: int,
) -> tuple[str, list[tuple[str, bytes, str]]]:
    if workload == "text":
        return "text", []
    if workload == "single-image":
        return "single-image", all_images[:1]
    if workload == "triple-image":
        return "triple-image", all_images[:3]
    # 确定性均匀采样，避免协程调度改变混合比例。
    has_image = ((request_index * 37) % 100) < image_percent
    if not has_image:
        return "text", []
    selected = all_images[:3]
    return ("triple-image" if len(selected) == 3 else "single-image"), selected


def extract_response(
    response: httpx.Response,
    *,
    use_stream: bool,
) -> tuple[dict[str, Any] | None, str | None, dict[str, float], dict[str, Any]]:
    body: dict[str, Any] | None = None
    error_code: str | None = None
    stages: dict[str, float] = {}
    vision: dict[str, Any] = {}
    if use_stream and response.status_code < 300:
        for event, data in parse_sse(response.text):
            if event in STAGE_EVENT_MAP and isinstance(data, dict):
                if isinstance(data.get("ms"), (int, float)):
                    stages[STAGE_EVENT_MAP[event]] = float(data["ms"])
                if event == "vision_completed":
                    vision = {
                        key: data.get(key)
                        for key in (
                            "degraded",
                            "degraded_reason",
                            "cache_hits",
                            "queue_wait_ms",
                            "inference_ms",
                            "gateway_total_ms",
                            "format_retries",
                        )
                    }
            elif event == "final" and isinstance(data, dict):
                candidate = data.get("response")
                if isinstance(candidate, dict):
                    body = candidate
            elif event == "error" and isinstance(data, dict):
                error = data.get("error")
                if isinstance(error, dict):
                    error_code = str(error.get("code") or "SSE_ERROR")
        if body is None and error_code is None:
            error_code = "SSE_NO_FINAL"
    else:
        try:
            candidate = response.json()
            body = candidate if isinstance(candidate, dict) else None
        except ValueError:
            body = None
    if body is not None:
        error = body.get("error")
        if isinstance(error, dict):
            error_code = str(error.get("code") or "BUSINESS_ERROR")
        elif body.get("status") == "error":
            error_code = str(body.get("code") or "BUSINESS_ERROR")
    return body, error_code, stages, vision


async def one(
    client: httpx.AsyncClient,
    *,
    path: str,
    backend_adapter: bool,
    base_text: str,
    cache_mode: str,
    request_index: int,
    workload: str,
    all_images: list[tuple[str, bytes, str]],
    image_percent: int,
    headers: dict[str, str],
    results: list[dict[str, Any]],
    use_stream: bool,
) -> None:
    request_id = f"load_{uuid.uuid4().hex[:16]}"
    kind, images = choose_images(workload, all_images, request_index, image_percent)
    text = build_request_text(base_text, cache_mode, request_id, bool(images))
    form = {"conversation_id": request_id, "text": text}
    if backend_adapter:
        form.update(
            {
                "x_user_id": f"load-user-{request_index}",
                "pet_info": '{"name":"压测宠物","species":"cat","age_value":2,"age_unit":"year"}',
            }
        )
    files = [("images", (name, content, media_type)) for name, content, media_type in images]
    request_headers = {
        **headers,
        "X-Request-Id": request_id,
        "Idempotency-Key": request_id,
    }
    started = time.perf_counter()
    try:
        response = await client.post(
            path,
            data=form,
            files=files or None,
            headers=request_headers,
        )
        body, error_code, stages, vision = extract_response(
            response, use_stream=use_stream
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        success = (
            200 <= response.status_code < 300
            and error_code is None
            and (body is None or body.get("status") != "error")
        )
        if not success and error_code is None:
            error_code = f"HTTP{response.status_code}"
        results.append(
            {
                "request_id": request_id,
                "kind": kind,
                "image_count": len(images),
                "ms": elapsed_ms,
                "http_status": response.status_code,
                "success": success,
                "error_code": error_code,
                "stages": stages,
                "vision": vision,
            }
        )
    except Exception as exc:  # noqa: BLE001 - 网络错误必须进入失败分布
        results.append(
            {
                "request_id": request_id,
                "kind": kind,
                "image_count": len(images),
                "ms": (time.perf_counter() - started) * 1000,
                "http_status": 0,
                "success": False,
                "error_code": type(exc).__name__,
                "stages": {},
                "vision": {},
            }
        )


def summarize_subset(results: list[dict[str, Any]], wall_s: float) -> dict[str, Any]:
    successful = [row for row in results if row["success"]]
    elapsed = [float(row["ms"]) for row in successful]
    failures = Counter(
        str(row.get("error_code") or f"HTTP{row['http_status']}")
        for row in results
        if not row["success"]
    )
    stage_names = sorted(
        {name for row in successful for name in (row.get("stages") or {})}
    )
    stage_percentiles = {}
    for name in stage_names:
        values = [
            float(row["stages"][name])
            for row in successful
            if name in row.get("stages", {})
        ]
        stage_percentiles[name] = {
            "samples": len(values),
            "p50_ms": round(percentile(values, 0.50)),
            "p95_ms": round(percentile(values, 0.95)),
            "p99_ms": round(percentile(values, 0.99)),
        }
    gateway_metrics = {}
    for name in ("queue_wait_ms", "inference_ms", "gateway_total_ms"):
        values = [
            float(row["vision"][name])
            for row in successful
            if isinstance((row.get("vision") or {}).get(name), (int, float))
        ]
        if values:
            gateway_metrics[name] = {
                "samples": len(values),
                "p50_ms": round(percentile(values, 0.50)),
                "p95_ms": round(percentile(values, 0.95)),
                "p99_ms": round(percentile(values, 0.99)),
            }
    return {
        "total": len(results),
        "ok": len(successful),
        "fail": len(results) - len(successful),
        "http_503": sum(1 for row in results if row["http_status"] == 503),
        "vision_degraded": sum(
            1 for row in results if bool((row.get("vision") or {}).get("degraded"))
        ),
        "vision_gateway_rejected": sum(
            1
            for row in results
            if "返回 503" in str((row.get("vision") or {}).get("degraded_reason") or "")
        ),
        "fail_by": dict(sorted(failures.items())),
        "p50_ms": round(percentile(elapsed, 0.50)),
        "p95_ms": round(percentile(elapsed, 0.95)),
        "p99_ms": round(percentile(elapsed, 0.99)),
        "throughput_rpm": round(len(successful) / wall_s * 60, 1) if wall_s > 0 else 0.0,
        "stages": stage_percentiles,
        "vision_gateway": gateway_metrics,
        "format_retries": sum(
            int((row.get("vision") or {}).get("format_retries") or 0)
            for row in results
        ),
        "cache_hits": sum(
            int((row.get("vision") or {}).get("cache_hits") or 0)
            for row in results
        ),
    }


async def run_round(
    *,
    base: str,
    path: str,
    backend_adapter: bool,
    concurrency: int,
    request_count: int,
    text: str,
    cache_mode: str,
    workload: str,
    images: list[tuple[str, bytes, str]],
    image_percent: int,
    headers: dict[str, str],
    timeout_seconds: float,
    use_stream: bool,
    warmup_requests: int,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    timeout = httpx.Timeout(timeout_seconds, connect=10.0)
    async with httpx.AsyncClient(base_url=base, timeout=timeout, trust_env=False) as client:
        if cache_mode == "warm" and images:
            warmup: list[dict[str, Any]] = []
            for index in range(warmup_requests):
                await one(
                    client,
                    path=path,
                    backend_adapter=backend_adapter,
                    base_text=text,
                    cache_mode=cache_mode,
                    request_index=index,
                    workload=workload,
                    all_images=images,
                    image_percent=image_percent,
                    headers=headers,
                    results=warmup,
                    use_stream=use_stream,
                )
            if any(not row["success"] for row in warmup):
                raise RuntimeError("热缓存预热请求失败，停止本档测试")
        started = time.perf_counter()
        for batch_start in range(0, request_count, concurrency):
            await asyncio.gather(
                *[
                    one(
                        client,
                        path=path,
                        backend_adapter=backend_adapter,
                        base_text=text,
                        cache_mode=cache_mode,
                        request_index=index,
                        workload=workload,
                        all_images=images,
                        image_percent=image_percent,
                        headers=headers,
                        results=results,
                        use_stream=use_stream,
                    )
                    for index in range(
                        batch_start,
                        min(batch_start + concurrency, request_count),
                    )
                ]
            )
        wall_s = time.perf_counter() - started
    summary = summarize_subset(results, wall_s)
    by_kind = {
        kind: summarize_subset([row for row in results if row["kind"] == kind], wall_s)
        for kind in sorted({row["kind"] for row in results})
    }
    return {
        "concurrency": concurrency,
        "wall_s": round(wall_s, 3),
        **summary,
        "by_kind": by_kind,
        "requests": results,
    }


async def validate_api_path(base: str, path: str) -> str | None:
    normal_path = path.removesuffix("/stream")
    try:
        async with httpx.AsyncClient(base_url=base, timeout=5.0, trust_env=False) as client:
            response = await client.get("/openapi.json")
            if response.status_code == 200:
                paths = response.json().get("paths", {})
                if normal_path not in paths and path not in paths:
                    return f"OpenAPI 中未找到 {normal_path}；请结合反向代理确认 --path"
    except Exception:  # noqa: BLE001 - 路径校验是提示，不阻断压测
        return "无法读取 OpenAPI；请手工确认反向代理后的真实 --path"
    return None


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.per < 1:
        parser.error("--per 必须大于 0")
    if not args.concurrency_levels or any(level < 1 for level in args.concurrency_levels):
        parser.error("--concurrency 中的值必须大于 0")
    if not 0 <= args.image_percent <= 100:
        parser.error("--image-percent 必须在 0 到 100 之间")
    if len(args.image) > 3:
        parser.error("问诊单次最多传入 3 张图片")
    needed = {"single-image": 1, "triple-image": 3, "mixed": 1}.get(args.workload, 0)
    if len(args.image) < needed:
        parser.error(f"--workload {args.workload} 至少需要 {needed} 个 --image")
    if args.cache_mode == "disabled" and not args.cache_disabled_confirmed:
        parser.error(
            "disabled 模式要求测试环境已设置 VISION_CACHE_ENABLED=false；"
            "确认后添加 --cache-disabled-confirmed"
        )
    if args.backend_adapter and args.stage_events:
        parser.error("后端适配路径不保证 SSE；请添加 --no-stage-events")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18100")
    parser.add_argument("--path", default="/api/v1/consult", help="真实问诊路径")
    parser.add_argument("--backend-adapter", action="store_true")
    parser.add_argument(
        "--workload",
        choices=("text", "single-image", "triple-image", "mixed"),
        default="text",
    )
    parser.add_argument(
        "--cache-mode", choices=("cold", "warm", "disabled"), default="cold"
    )
    parser.add_argument("--cache-disabled-confirmed", action="store_true")
    parser.add_argument("--warmup-requests", type=int, default=1)
    parser.add_argument("--image-percent", type=int, default=20, help="mixed 中图片请求比例")
    parser.add_argument("--concurrency", default="1,2,4,8")
    parser.add_argument("--per", type=int, default=20, help="每档请求数")
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--text", default="眼睛分泌物多，三天了，精神还行")
    parser.add_argument("--token", default=None)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--json-out", default=None)
    parser.add_argument(
        "--no-stage-events",
        dest="stage_events",
        action="store_false",
        help="使用普通 JSON 端点，不采集 SSE 阶段耗时",
    )
    parser.set_defaults(stage_events=True)
    args = parser.parse_args()
    try:
        args.concurrency_levels = [
            int(value.strip()) for value in args.concurrency.split(",") if value.strip()
        ]
    except ValueError:
        parser.error("--concurrency 必须是逗号分隔的整数")
    validate_args(parser, args)

    images = [
        (Path(path).name, Path(path).read_bytes(), media_type_for(path))
        for path in args.image
    ]
    headers = (
        {"Authorization": f"Bearer {args.token}"}
        if args.token
        else {"X-User-Id": "load-test-user"}
    )
    request_path = args.path.rstrip("/") + "/stream" if args.stage_events else args.path
    warning = await validate_api_path(args.base, request_path)
    if warning:
        print(f"路径提示: {warning}")
    print(
        f"目标={args.base}{request_path} workload={args.workload} "
        f"cache={args.cache_mode} per={args.per} concurrency={args.concurrency_levels}"
    )
    print(
        f"{'并发':>4} {'成功':>6} {'失败':>5} {'503':>5} {'视觉拒绝':>8} {'视觉降级':>8} "
        f"{'P50(ms)':>9} {'P95(ms)':>9} {'P99(ms)':>9} {'吞吐/min':>10}"
    )
    summaries: list[dict[str, Any]] = []
    for concurrency in args.concurrency_levels:
        result = await run_round(
            base=args.base,
            path=request_path,
            backend_adapter=args.backend_adapter,
            concurrency=concurrency,
            request_count=args.per,
            text=args.text,
            cache_mode=args.cache_mode,
            workload=args.workload,
            images=images,
            image_percent=args.image_percent,
            headers=headers,
            timeout_seconds=args.timeout,
            use_stream=args.stage_events,
            warmup_requests=args.warmup_requests,
        )
        summaries.append(result)
        print(
            f"{concurrency:>4} {result['ok']:>6} {result['fail']:>5} "
            f"{result['http_503']:>5} {result['vision_gateway_rejected']:>8} "
            f"{result['vision_degraded']:>8} "
            f"{result['p50_ms']:>9} {result['p95_ms']:>9} {result['p99_ms']:>9} "
            f"{result['throughput_rpm']:>10.1f}"
        )
        if result["fail_by"]:
            print("     失败分布:", json.dumps(result["fail_by"], ensure_ascii=False))
        if result["stages"]:
            print("     阶段耗时:", json.dumps(result["stages"], ensure_ascii=False))
        if result["vision_gateway"]:
            print("     视觉网关:", json.dumps(result["vision_gateway"], ensure_ascii=False))
        if args.workload == "mixed":
            compact = {
                kind: {
                    key: values[key]
                    for key in ("total", "ok", "fail", "http_503", "p50_ms", "p95_ms", "p99_ms")
                }
                for kind, values in result["by_kind"].items()
            }
            print("     分类结果:", json.dumps(compact, ensure_ascii=False))
    if args.json_out:
        output = {
            "metadata": {
                "base": args.base,
                "path": request_path,
                "workload": args.workload,
                "cache_mode": args.cache_mode,
                "image_percent": args.image_percent,
                "per": args.per,
            },
            "rounds": summaries,
        }
        Path(args.json_out).write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"完整逐请求结果已写入 {args.json_out}")
    print("GPU/vLLM 指标需与本脚本时间窗同场采集，不能从 API 延迟反推。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
