"""宽容 JSON 提取与解析（v5 §12.4 输出解析策略）

策略：原始响应 → 提取首个 JSON 对象（支持 ```json 代码块）→ JSON 解析
→ Pydantic 校验。失败抛 ModelOutputValidationError，由上层重试/降级。
"""
from __future__ import annotations

import json
import logging
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.core.exceptions import ModelOutputValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# 匹配 ```json ... ``` 或裸 JSON 对象（含嵌套）
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_candidate(candidate: str) -> dict | None:
    """尝试解析 JSON 候选; 失败返回 None(不抛异常)。"""
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def extract_json_object(raw: str) -> dict:
    """从模型输出中提取第一个 JSON 对象；失败抛 ModelOutputValidationError。"""
    if not raw:
        raise ModelOutputValidationError("模型输出为空")
    match = _FENCE_RE.search(raw)
    if match:
        candidate = match.group(1)
    else:
        bare = _OBJECT_RE.search(raw)
        candidate = bare.group(0) if bare else None
    if not candidate:
        logger.warning("模型输出无 JSON 块: %r", raw[:200])
        raise ModelOutputValidationError("模型输出不含 JSON")
    obj = _parse_candidate(candidate)
    if obj is None:
        # 二次尝试: 本地模型偶发把 JSON 输出为转义字符串({\n  \"summary\"...})
        unescaped = (
            candidate.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
        obj = _parse_candidate(unescaped)
    if obj is None:
        logger.warning("JSON 解析失败: %r", candidate[:200])
        raise ModelOutputValidationError("JSON 解析失败")
    return obj


def parse_model_output(raw: str, model: type[T]) -> T:
    """提取 JSON 并用 Pydantic 校验；任何失败统一抛 ModelOutputValidationError。"""
    obj = extract_json_object(raw)
    try:
        return model.model_validate(obj)
    except ValidationError as exc:
        logger.warning("模型输出校验失败: %s", exc)
        raise ModelOutputValidationError(f"模型输出校验失败: {exc}") from exc