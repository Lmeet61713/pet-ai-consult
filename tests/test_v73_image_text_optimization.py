from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.consult_agent import _should_retry_knowledge_failure
from app.core.config import Settings
from app.core.exceptions import KnowledgeConsultUnavailable, QueueBusyError
from app.tasks.mq import ConsultMessage, parse_consult_message
from app.tasks.models import Base
from app.tasks.service import TaskService
from app.vision_server import VisionRuntime
from scripts.load_matrix import (
    build_request_text,
    extract_response,
    parse_sse,
    summarize_subset,
)


def test_settings_classified_admission_requires_both_limits() -> None:
    with pytest.raises(ValueError, match="configured together"):
        Settings(app_env="test", text_max_active=24, _env_file=None)


def test_settings_allows_benchmarked_worker_candidates_up_to_32() -> None:
    assert Settings(
        app_env="test", consult_worker_count=20, _env_file=None
    ).consult_worker_count == 20
    with pytest.raises(ValueError):
        Settings(app_env="test", consult_worker_count=33, _env_file=None)


def test_consult_message_accepts_v73_routing_fields() -> None:
    message = ConsultMessage(
        task_id=1,
        request_id="req-v73",
        priority="P1",
        fast_path=False,
        pre_answered=False,
        task_kind="image",
        image_count=3,
    )
    assert parse_consult_message(message.model_dump_json()).task_kind == "image"


def test_consult_message_keeps_v72_payload_compatible() -> None:
    message = ConsultMessage.model_validate(
        {
            "task_id": 1,
            "request_id": "req-v72",
            "priority": "P1",
            "fast_path": False,
            "pre_answered": False,
        }
    )
    assert (message.task_kind, message.image_count) == ("text", 0)


def test_settings_rejects_image_slots_smaller_than_request_limit() -> None:
    with pytest.raises(ValueError, match="cannot be smaller"):
        Settings(
            app_env="test",
            text_max_active=24,
            image_max_active=6,
            image_max_active_slots=5,
            _env_file=None,
        )


@pytest.mark.asyncio
async def test_classified_admission_keeps_text_and_image_capacity_separate() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    service = TaskService(async_sessionmaker(engine, expire_on_commit=False))
    common = {
        "tenant_id": "tenant",
        "user_id": "user",
        "text_max_active": 1,
        "image_max_active": 1,
        "image_max_active_slots": 3,
    }
    await service.register_task(
        request_id="text-1",
        conversation_id="text-1",
        task_kind="text",
        **common,
    )
    with pytest.raises(QueueBusyError):
        await service.register_task(
            request_id="text-2",
            conversation_id="text-2",
            task_kind="text",
            **common,
        )
    image_id = await service.register_task(
        request_id="image-1",
        conversation_id="image-1",
        task_kind="image",
        image_count=3,
        **common,
    )
    task = await service.get_task(image_id)
    assert task is not None
    assert task.task_kind == "image"
    assert task.image_count == 3
    assert service.admission_snapshot() == {"text_active": 1}
    await engine.dispose()


@pytest.mark.asyncio
async def test_image_slot_limit_is_weighted_by_image_count() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    service = TaskService(async_sessionmaker(engine, expire_on_commit=False))
    common = {
        "tenant_id": "tenant",
        "user_id": "user",
        "task_kind": "image",
        "text_max_active": 10,
        "image_max_active": 4,
        "image_max_active_slots": 4,
    }
    await service.register_task(
        request_id="single",
        conversation_id="single",
        image_count=1,
        **common,
    )
    with pytest.raises(QueueBusyError, match="图片分析槽位"):
        await service.register_task(
            request_id="triple-a",
            conversation_id="triple-a",
            image_count=3,
            **common,
        )
        await service.register_task(
            request_id="triple-b",
            conversation_id="triple-b",
            image_count=1,
            **common,
        )
    assert service.admission_snapshot() == {"image_slots": 1}
    await engine.dispose()


@pytest.mark.asyncio
async def test_vision_health_reports_effective_runtime_configuration(monkeypatch) -> None:
    monkeypatch.setenv("VISION_CONCURRENCY", "6")
    monkeypatch.setenv("VISION_MAX_QUEUE_SIZE", "16")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "Qwen3.5-4B"}]})

    client = httpx.AsyncClient(
        base_url="http://vision-upstream",
        transport=httpx.MockTransport(handler),
    )
    runtime = VisionRuntime(
        upstream_url="http://vision-upstream",
        model_name="Qwen3.5-4B",
        client=client,
    )
    await runtime.start()
    health = await runtime.health()
    assert runtime._semaphore._value == 6
    assert health == {
        "status": "ok",
        "upstream": "http://vision-upstream",
        "model": "Qwen3.5-4B",
        "concurrency": 6,
        "max_queue_size": 16,
        "queue_size": 0,
    }
    await runtime.stop()
    await client.aclose()


def test_vision_runtime_fails_fast_for_invalid_queue(monkeypatch) -> None:
    monkeypatch.setenv("VISION_MAX_QUEUE_SIZE", "0")
    with pytest.raises(ValueError, match="VISION_MAX_QUEUE_SIZE"):
        VisionRuntime()


def test_cold_cache_text_changes_key_but_warm_text_is_fixed() -> None:
    assert build_request_text("同一问题", "cold", "req-a", True) != build_request_text(
        "同一问题", "cold", "req-b", True
    )
    assert build_request_text("同一问题", "warm", "req-a", True) == "同一问题"
    assert build_request_text("同一问题", "cold", "req-a", False) == "同一问题"


def test_sse_stage_metrics_and_final_response_are_extracted() -> None:
    final = {
        "request_id": "r1",
        "conversation_id": "c1",
        "status": "success",
    }
    text = (
        'event: vision_completed\ndata: {"ms":5100,"queue_wait_ms":100,'
        '"inference_ms":4800,"degraded":false,"cache_hits":0}\n\n'
        'event: answer_generated\ndata: {"ms":7200}\n\n'
        f"event: final\ndata: {json.dumps({'response': final})}\n\n"
    )
    assert len(parse_sse(text)) == 3
    response = httpx.Response(200, text=text)
    body, error, stages, vision = extract_response(response, use_stream=True)
    assert body == final
    assert error is None
    assert stages == {"vision_ms": 5100.0, "generate_ms": 7200.0}
    assert vision["queue_wait_ms"] == 100


def test_summary_excludes_http_503_from_success_latency() -> None:
    rows = [
        {
            "success": True,
            "ms": 1000,
            "http_status": 200,
            "error_code": None,
            "stages": {},
            "vision": {},
        },
        {
            "success": False,
            "ms": 99_000,
            "http_status": 503,
            "error_code": "QUEUE_BUSY",
            "stages": {},
            "vision": {},
        },
    ]
    summary = summarize_subset(rows, wall_s=2.0)
    assert summary["ok"] == 1
    assert summary["http_503"] == 1
    assert summary["p50_ms"] == 1000
    assert summary["p99_ms"] == 1000
def test_knowledge_timeout_does_not_trigger_retry_storm() -> None:
    assert _should_retry_knowledge_failure(
        KnowledgeConsultUnavailable("知识问诊超时")
    ) is False
    assert _should_retry_knowledge_failure(
        KnowledgeConsultUnavailable("知识问诊流式超时")
    ) is False
    assert _should_retry_knowledge_failure(
        KnowledgeConsultUnavailable("知识问诊返回 503")
    ) is True
