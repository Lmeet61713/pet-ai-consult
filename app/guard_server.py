"""Qwen3Guard service for the pet-consult moderation contract.

The model is loaded lazily so importing this module remains safe on machines
without CUDA.  A bounded asyncio queue serializes GPU generation by default;
the HTTP contract stays independent from the model implementation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("pet-consult.guard")

LABELS = {"Safe", "Unsafe", "Controversial"}
CATEGORY_PATTERN = re.compile(
    r"Violent|Non-violent Illegal Acts|Sexual Content or Sexual Acts|PII|"
    r"Suicide & Self-Harm|Unethical Acts|Politically Sensitive Topics|"
    r"Copyright Violation|Jailbreak|None",
)
SAFETY_PATTERN = re.compile(r"^\s*Safety:\s*(Safe|Unsafe|Controversial)\s*$", re.MULTILINE)
CATEGORIES_LINE_PATTERN = re.compile(r"^\s*Categories:\s*(.*?)\s*$", re.MULTILINE)


class ModerateRequest(BaseModel):
    scene: Literal["pet_consult_input", "pet_consult_output"]
    text: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(default="guard", max_length=128)


class ModerateResponse(BaseModel):
    blocked: bool
    verdict: Literal["Safe", "Unsafe", "Controversial", "Review"]
    categories: list[str]
    parse_ok: bool
    scene: str
    request_id: str


class GuardOverloaded(RuntimeError):
    """The bounded request queue cannot accept another moderation request."""


def parse_guard_output(content: str) -> tuple[str, list[str], bool]:
    """Parse the official Qwen3Guard-Gen text format conservatively."""
    label_match = SAFETY_PATTERN.search(content)
    categories_match = CATEGORIES_LINE_PATTERN.search(content)
    label = label_match.group(1) if label_match else "Review"
    category_text = categories_match.group(1) if categories_match else ""
    categories = [item for item in CATEGORY_PATTERN.findall(category_text) if item != "None"]
    categories = list(dict.fromkeys(categories))
    shape_ok = (label == "Safe" and not categories) or (
        label in {"Unsafe", "Controversial"} and bool(categories)
    )
    parse_ok = label in LABELS and categories_match is not None and shape_ok
    return label, categories, parse_ok


def moderation_response(
    content: str,
    *,
    scene: str,
    request_id: str,
) -> ModerateResponse:
    label, categories, parse_ok = parse_guard_output(content)
    if not parse_ok:
        return ModerateResponse(
            blocked=True,
            verdict="Review",
            categories=categories,
            parse_ok=False,
            scene=scene,
            request_id=request_id,
        )
    return ModerateResponse(
        blocked=label in {"Unsafe", "Controversial"},
        verdict=label,  # type: ignore[arg-type]
        categories=categories,
        parse_ok=True,
        scene=scene,
        request_id=request_id,
    )


@dataclass
class _Job:
    text: str
    scene: str
    request_id: str
    future: asyncio.Future[ModerateResponse]


class GuardRuntime:
    """Loads Qwen3Guard and owns the bounded single-GPU inference queue."""

    def __init__(
        self,
        *,
        model_path: str | None = None,
        max_new_tokens: int | None = None,
        max_queue_size: int | None = None,
        request_timeout_seconds: float | None = None,
        worker_count: int | None = None,
        loader: Callable[[str], None] | None = None,
        infer: Callable[[str], str] | None = None,
    ) -> None:
        self.model_path = model_path or os.getenv(
            "GUARD_MODEL_PATH", "/root/autodl-tmp/models/Qwen3Guard-Gen-0.6B"
        )
        self.max_new_tokens = max_new_tokens or int(os.getenv("GUARD_MAX_NEW_TOKENS", "32"))
        self.max_queue_size = max_queue_size or int(os.getenv("GUARD_MAX_QUEUE_SIZE", "16"))
        self.request_timeout_seconds = request_timeout_seconds or float(
            os.getenv("GUARD_REQUEST_TIMEOUT_SECONDS", "1.25")
        )
        self.worker_count = worker_count or int(os.getenv("GUARD_WORKERS", "1"))
        self._loader = loader
        self._infer_override = infer
        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=self.max_queue_size)
        self._workers: list[asyncio.Task[None]] = []
        self._started = False
        self._model: Any = None
        self._tokenizer: Any = None
        self._torch: Any = None
        self._device: Any = None

    @property
    def ready(self) -> bool:
        return self._started

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        if self._started:
            return
        if self._loader is not None:
            await asyncio.to_thread(self._loader, self.model_path)
        else:
            await asyncio.to_thread(self._load_model)
        self._started = True
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.worker_count)]

    async def stop(self) -> None:
        self._started = False
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._model = None
        self._tokenizer = None

    def _load_model(self) -> None:
        import torch
        import transformers
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
        dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32
        dtype_argument = "dtype" if int(transformers.__version__.split(".")[0]) >= 5 else "torch_dtype"
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_path,
            device_map="auto",
            **{dtype_argument: dtype},
        )
        self._model.eval()
        self._device = next(self._model.parameters()).device
        self._infer_sync(
            os.getenv("GUARD_WARMUP_TEXT", "你好"),
            "pet_consult_input",
        )
        logger.info("Guard model ready path=%s device=%s", self.model_path, self._device)

    def _infer_sync(self, text: str, scene: str) -> str:
        if self._infer_override is not None:
            return self._infer_override(text)
        if scene == "pet_consult_output":
            messages = [
                {"role": "user", "content": "请回答这个宠物健康咨询。"},
                {"role": "assistant", "content": text},
            ]
        else:
            messages = [{"role": "user", "content": text}]
        rendered = self._tokenizer.apply_chat_template(
            messages,
            tokenize=False,
        )
        inputs = self._tokenizer([rendered], return_tensors="pt").to(self._device)
        with self._torch.inference_mode():
            generated = self._model.generate(
                **inputs,
                max_new_tokens=self.max_new_tokens,
                do_sample=False,
            )
        prompt_length = inputs["input_ids"].shape[1]
        output_ids = generated[0][prompt_length:].tolist()
        return self._tokenizer.decode(output_ids, skip_special_tokens=True)

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                content = await asyncio.to_thread(self._infer_sync, job.text, job.scene)
                result = moderation_response(
                    content, scene=job.scene, request_id=job.request_id
                )
                if not job.future.done():
                    job.future.set_result(result)
            except (RuntimeError, ValueError, TypeError, OSError, KeyError, IndexError):
                logger.exception("Guard inference failed")
                if not job.future.done():
                    job.future.set_result(
                        ModerateResponse(
                            blocked=True,
                            verdict="Review",
                            categories=[],
                            parse_ok=False,
                            scene=job.scene,
                            request_id=job.request_id,
                        )
                    )
            finally:
                self._queue.task_done()

    async def submit(self, request: ModerateRequest) -> ModerateResponse:
        if not self._started:
            raise RuntimeError("Guard model is not ready")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ModerateResponse] = loop.create_future()
        try:
            self._queue.put_nowait(
                _Job(request.text, request.scene, request.request_id, future)
            )
        except asyncio.QueueFull as exc:
            raise GuardOverloaded("Guard queue is full") from exc
        return await asyncio.wait_for(future, timeout=self.request_timeout_seconds)


def create_app(runtime: GuardRuntime | None = None) -> FastAPI:
    runtime = runtime or GuardRuntime()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="Pet Consult Guard", version="1.0.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        if not runtime.ready:
            raise HTTPException(status_code=503, detail="Guard model is not ready")
        return {
            "status": "ok",
            "model": "Qwen3Guard-Gen-0.6B",
            "queue_size": runtime.queue_size,
        }

    @app.post("/v1/moderate", response_model=ModerateResponse)
    async def moderate(request: ModerateRequest) -> ModerateResponse:
        try:
            return await runtime.submit(request)
        except GuardOverloaded as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Guard inference timed out") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return app


app = create_app()
