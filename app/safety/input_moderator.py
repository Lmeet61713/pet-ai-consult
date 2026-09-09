"""输入通用审核（v6.3 §13.1 / §13.1.1 场景化）

规则层只拒绝明确恶意/越权：绕过限制、索要危险剂量、与宠物问诊无关的高风险请求。
医疗求助中的"出血、伤口、车祸、误食"等描述不拦截（走急症流程）。
模型层（Qwen3Guard）结果由 moderation_service 结合 scene 和本规则判定
should_refuse_medical_request —— 只有恶意请求才 refuse。
"""
from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH_RE = re.compile("[\u200b-\u200d\u2060\ufeff]")
_WHITESPACE_RE = re.compile(r"\s+")

# 明确要求绕过限制（越狱/注入）
_BYPASS_RE = re.compile(
    r"忽略.{0,8}(提示|规则|限制|指令)|无视.{0,8}(规则|安全)|"
    r"roleplay|假装你是|进入角色|jailbreak|bypass",
    re.IGNORECASE,
)

# 索要危险剂量 / 毒物用途（恶意）
_MALICIOUS_RE = re.compile(
    r"(老鼠药|毒药|毒鼠强|杀虫剂|除草剂|百草枯).{0,10}(剂量|多少|怎么用|怎么喂)"
    r"|(多少克|多少mg|剂量|怎么用|怎么喂).{0,10}(毒|致死|老鼠药|毒鼠强|杀虫剂|除草剂|百草枯)"
)

# 鼓励虐待、教唆伤害
_ABUSE_RE = re.compile(
    r"怎么虐待|如何折磨|打.{0,4}(猫|狗).{0,4}(不会|不留下)|故意.{0,4}(弄伤|烫伤|饿死)|"
    r"虐待.{0,6}(视频|取乐|好玩)"
)


def _normalize_for_rules(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = _ZERO_WIDTH_RE.sub("", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip().lower()


class InputModerator:
    """规则层输入审核；返回 (blocked, reason)。

    只拦截明确恶意/越权请求；医疗描述（伤口/出血/车祸）不在此列。
    """

    def check(self, text: str) -> tuple[bool, str]:
        normalized = _normalize_for_rules(text)
        if not normalized:
            return False, ""
        if _BYPASS_RE.search(normalized):
            return True, "prompt_bypass_attempt"
        if _MALICIOUS_RE.search(normalized):
            return True, "malicious_drug_request"
        if _ABUSE_RE.search(normalized):
            return True, "animal_abuse_request"
        return False, ""

    @staticmethod
    def is_medical_request(text: str) -> bool:
        """医疗求助特征词（用于 Guard 结果映射，v6.3 §13.1.1）。"""
        normalized = _normalize_for_rules(text)
        return any(
            kw in normalized for kw in (
                "出血", "伤口", "车祸", "误食", "呕吐", "腹泻", "抽搐", "呼吸困难",
                "外伤", "摔", "尿", "吐", "流血", "药",
            )
        )
