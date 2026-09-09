"""医疗检查 Prompt（v5 §13.3 可选 LLM 兜底；第一版以规则检查为主）

规则 MedicalSafetyChecker 是第一道，本 Prompt 用于模型层交叉复查（预留）。
"""
from __future__ import annotations

from app.prompts.registry import PromptSpec

PROMPT_ID = "medical_review"
PROMPT_VERSION = "v1.0.0"

SYSTEM = (
    "你是宠物医疗安全审查员。请审查下面这段宠物问诊回答，输出 JSON：\n"
    '{"passed": bool, "violations": [string], "severity": "pass|rewrite|review|go_vet"}\n'
    "重点检查：是否输出具体剂量/频率/疗程、是否确诊断言、是否高风险人用药、"
    "是否遗漏已命中急症、是否过度保证（'肯定没事''不用看医生'）、"
    "是否自相矛盾、是否缺少免责声明。"
)

SPEC = PromptSpec(prompt_id=PROMPT_ID, version=PROMPT_VERSION, template=SYSTEM)
