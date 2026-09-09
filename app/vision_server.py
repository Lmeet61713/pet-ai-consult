"""VisionGateway: bounded proxy to the shared vLLM OpenAI endpoint.

The gateway deliberately does not import Transformers or load model weights.
Qwen3.5 is owned by the single consultation vLLM process on port 8101.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("pet-consult.vision")

GUIDED_VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "image_quality": {"type": "string", "enum": ["good", "poor", "unusable"]},
        "species_guess": {"type": "string"},
        "body_parts": {"type": "array", "items": {"type": "string"}},
        "observations": {"type": "array", "items": {"type": "string"}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "model_confidence": {"type": "number"},
        "needs_more_images": {"type": "boolean"},
        "missing_views": {"type": "array", "items": {"type": "string"}},
        "suggested_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["image_quality"],
}


VISION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "image_quality": {"type": "string", "enum": ["good", "poor", "unusable"]},
        "species_guess": {"type": "string"},
        "body_parts": {"type": "array", "items": {"type": "string"}},
        "observations": {"type": "array", "items": {"type": "string"}},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "model_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "needs_more_images": {"type": "boolean"},
        "missing_views": {"type": "array", "items": {"type": "string"}},
        "suggested_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["image_quality"],
    "additionalProperties": False,
}


class VisionRequest(BaseModel):
    scene: str = Field(default="pet_consult_image", min_length=1, max_length=64)
    request_id: str = Field(default="vision", min_length=1, max_length=128)
    priority: int = Field(default=0, ge=-100, le=100)
    timeout_seconds: float = Field(default=15.0, gt=0.1, le=120.0)
    max_tokens: int = Field(default=512, ge=1, le=4096)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=16)

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if any(message.get("role") not in {"system", "user", "assistant", "tool"}
               for message in value):
            raise ValueError("messages contains an invalid role")
        return value


class VisionResponse(BaseModel):
    content: str
    queue_wait_ms: int = Field(ge=0)
    inference_ms: int = Field(ge=0)
    total_ms: int = Field(ge=0)
    request_id: str


class VisionOverloaded(RuntimeError):
    """The bounded gateway queue is full."""


class VisionUpstreamError(RuntimeError):
    """The vLLM endpoint returned an unusable response."""


class VisionRuntime:
    def __init__(
        self,
        *,
        upstream_url: str | None = None,
        model_name: str | None = None,
        concurrency: int | None = None,
        max_queue_size: int | None = None,
        max_request_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.upstream_url = (
            upstream_url
            if upstream_url is not None
            else os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8101")
        ).rstrip("/")
        self.model_name = (
            model_name
            if model_name is not None
            else os.getenv("VLLM_MODEL_NAME", "Qwen3.5-4B")
        )
        self.max_queue_size = (
            max_queue_size
            if max_queue_size is not None
            else int(os.getenv("VISION_MAX_QUEUE_SIZE", "12"))
        )
        self.max_request_seconds = (
            max_request_seconds
            if max_request_seconds is not None
            else float(os.getenv("VISION_MAX_REQUEST_SECONDS", "45"))
        )
        self.concurrency = (
            concurrency
            if concurrency is not None
            else int(os.getenv("VISION_CONCURRENCY", "4"))
        )
        if not self.upstream_url:
            raise ValueError("VLLM_BASE_URL must not be empty")
        if not self.model_name.strip():
            raise ValueError("VLLM_MODEL_NAME must not be empty")
        if not 1 <= self.concurrency <= 16:
            raise ValueError("VISION_CONCURRENCY must be between 1 and 16")
        if not 1 <= self.max_queue_size <= 1024:
            raise ValueError("VISION_MAX_QUEUE_SIZE must be between 1 and 1024")
        if self.max_request_seconds <= 0:
            raise ValueError("VISION_MAX_REQUEST_SECONDS must be greater than 0")
        self._client = client
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._pending = 0
        self._started = False

    @property
    def ready(self) -> bool:
        return self._started

    @property
    def queue_size(self) -> int:
        return self._pending

    async def start(self) -> None:
        if self._started:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.upstream_url)
        self._started = True
        logger.info(
            "vision_gateway_started upstream=%s model=%s concurrency=%s max_queue_size=%s "
            "max_request_seconds=%s",
            self.upstream_url,
            self.model_name,
            self.concurrency,
            self.max_queue_size,
            self.max_request_seconds,
        )

    async def stop(self) -> None:
        self._started = False
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None if self._owns_client else self._client

    async def health(self) -> dict[str, Any]:
        if not self._started or self._client is None:
            raise VisionUpstreamError("VisionGateway is not ready")
        try:
            response = await self._client.get("/v1/models", timeout=2.0)
            response.raise_for_status()
            data = response.json()
            models = data.get("data") if isinstance(data, dict) else None
            if not isinstance(models, list):
                raise VisionUpstreamError("vLLM /v1/models returned invalid JSON")
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise VisionUpstreamError(f"vLLM unavailable: {exc}") from exc
        return {
            "status": "ok",
            "upstream": self.upstream_url,
            "model": self.model_name,
            "concurrency": self.concurrency,
            "max_queue_size": self.max_queue_size,
            "queue_size": self.queue_size,
        }

    async def submit(self, request: VisionRequest) -> VisionResponse:
        if not self._started or self._client is None:
            raise VisionUpstreamError("VisionGateway is not ready")
        if self._pending >= self.max_queue_size:
            raise VisionOverloaded("Vision queue is full")

        self._pending += 1
        total_started = time.perf_counter()
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=request.timeout_seconds)
            except TimeoutError as exc:
                raise VisionOverloaded("Vision queue wait timed out") from exc
            queue_wait_ms = round((time.perf_counter() - total_started) * 1000)
            try:
                elapsed = time.perf_counter() - total_started
                remaining = min(
                    request.timeout_seconds - elapsed,
                    self.max_request_seconds,
                )
                if remaining <= 0:
                    raise TimeoutError("Vision request timed out in queue")
                payload = {
                    "model": self.model_name,
                    "messages": request.messages,
                    # vLLM priority scheduling consumes this extension field.
                    "priority": request.priority,
                    "max_tokens": request.max_tokens,
                    "temperature": 0,
                    # Qwen3.5 思考型模型：关闭思考输出，否则 Thinking Process 混入 JSON
                    "chat_template_kwargs": {"enable_thinking": False},
                    # vLLM 0.26 的 guided_json 不支持 additionalProperties/union；
                    # 用简化 schema（解析层 VisionOutputParser 仍按完整契约校验）。
                    "guided_json": GUIDED_VISION_SCHEMA,
                }
                headers = {"X-Request-ID": request.request_id}
                inference_started = time.perf_counter()
                try:
                    async with asyncio.timeout(remaining):
                        response = await self._client.post(
                            "/v1/chat/completions",
                            json=payload,
                            headers=headers,
                            timeout=httpx.Timeout(remaining, connect=3.0),
                        )
                except httpx.TimeoutException as exc:
                    raise TimeoutError("vLLM inference timed out") from exc
                except TimeoutError as exc:
                    raise TimeoutError("vLLM inference timed out") from exc
                except httpx.HTTPError as exc:
                    raise VisionUpstreamError(f"vLLM request failed: {exc}") from exc
                inference_ms = round((time.perf_counter() - inference_started) * 1000)
                if response.status_code >= 500:
                    raise VisionUpstreamError(f"vLLM returned {response.status_code}")
                if response.status_code >= 400:
                    logger.error(
                        "vision_upstream_rejected status=%s body=%s",
                        response.status_code,
                        response.text[:400],
                    )
                    raise VisionUpstreamError(f"vLLM rejected request ({response.status_code})")
                try:
                    body = response.json()
                    content = body["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise VisionUpstreamError("vLLM response has no message content") from exc
                if isinstance(content, list):
                    content = "".join(
                        str(item.get("text", "")) if isinstance(item, dict) else str(item)
                        for item in content
                    )
                if not isinstance(content, str) or not content.strip():
                    raise VisionUpstreamError("vLLM response content is empty")
                return VisionResponse(
                    content=content,
                    queue_wait_ms=queue_wait_ms,
                    inference_ms=inference_ms,
                    total_ms=round((time.perf_counter() - total_started) * 1000),
                    request_id=request.request_id,
                )
            finally:
                self._semaphore.release()
        finally:
            self._pending -= 1


def create_app(runtime: VisionRuntime | None = None) -> FastAPI:
    runtime = runtime or VisionRuntime()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="Pet Consult VisionGateway", version="1.0.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        try:
            return await runtime.health()
        except VisionUpstreamError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/v1/vision", response_model=VisionResponse)
    async def vision(request: VisionRequest) -> VisionResponse:
        try:
            return await runtime.submit(request)
        except VisionOverloaded as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except VisionUpstreamError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app


app = create_app()
