"""Prompt 注册表（v5 §12.1：每个 Prompt 带 ID + Version，请求日志保存）"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptSpec:
    prompt_id: str
    version: str
    template: str


class PromptRegistry:
    """注册并按 ID 取 Prompt。升级 = 注册新版本，旧版本保留可回溯。"""

    def __init__(self) -> None:
        self._prompts: dict[str, PromptSpec] = {}

    def register(self, spec: PromptSpec) -> None:
        self._prompts[spec.prompt_id] = spec

    def get(self, prompt_id: str) -> PromptSpec:
        if prompt_id not in self._prompts:
            raise KeyError(f"未知 Prompt: {prompt_id}")
        return self._prompts[prompt_id]

    def version(self, prompt_id: str) -> str:
        return self.get(prompt_id).version
