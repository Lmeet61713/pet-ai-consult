"""
急症规则引擎（v6.3 §13.2）

负责宠物问诊中的急症判断，分两个阶段：

1. precheck_text（前置预判）：纯规则文字匹配，流程最前置（不调外部服务）。
   命中后记录强制急症标记，后续任何失败都走固定急症模板。

2. evaluate（最终评估）：合并文字预判 + 图片 red_flags + 宠物档案。
   脆弱宠物（幼/老/病弱）会做风险上调（bump）。

风险级别：
- EMERGENCY：固定短路，直接输出急症指导
- HIGH：切换紧急指导模式
- MEDIUM/LOW：正常问诊流程
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from app.core.config import CONFIG_DIR, Settings
from app.core.constants import RISK_ORDER, RiskLevel, VetUrgency, VET_URGENCY_ORDER
from app.schemas.image import VisionFinding
from app.schemas.pet import PetInfo
from app.schemas.safety import EmergencyResult

logger = logging.getLogger(__name__)

# 图片 red_flags 关键词兜底（未覆盖规则的 flag 按此识别）
_RED_FLAG_KEYWORDS = (
    "出血", "呼吸", "抽搐", "昏迷", "外伤", "瘫痪", "尿闭", "误食", "中毒", "呕吐", "便血", "失禁",
)

_NEGATED_BEFORE_RE = re.compile(
    r"(?:没有|没|无|未|否认|并无|不存在|不是|不再|未见|未出现|没有出现|没出现)"
    r"[^，。；！？]{0,6}$"
)


def _is_negated(text: str, start: int) -> bool:
    """识别紧邻症状前的常见否定表达，避免“没有呼吸困难”误报。"""
    return _NEGATED_BEFORE_RE.search(text[max(0, start - 14):start]) is not None


class EmergencyRule:
    """单条急症规则，从 YAML 配置加载。

    匹配方式：关键词（any_keywords）或正则模式（context_patterns）。
    匹配时自动识别否定表达，避免"没有呼吸困难"误报为急症。
    """

    def __init__(self, raw: dict):
        self.id = raw["id"]  # 规则唯一标识
        self.level = RiskLevel(raw.get("level", "high"))  # 风险等级
        self.vet_urgency = VetUrgency(raw.get("vet_urgency", VetUrgency.URGENT.value))  # 就医紧迫度
        self.species = raw.get("species", [])  # 适用物种（空列表表示全部）
        self.force_urgent_guidance = bool(raw.get("force_urgent_guidance", False))  # 是否强制急症模式
        self.immediate_actions: list[str] = raw.get("immediate_actions", [])  # 建议立即行动
        self.avoid_actions: list[str] = raw.get("avoid_actions", [])  # 禁止事项
        self._keywords: list[str] = raw.get("any_keywords", [])  # 触发关键词
        self._patterns: list[re.Pattern[str]] = [  # 触发正则模式
            re.compile(p) for p in raw.get("context_patterns", [])
        ]

    def search(self, text: str) -> bool:
        """在文本中搜索该规则的关键词或正则模式，自动识别否定。"""
        for keyword in self._keywords:
            start = 0
            while (index := text.find(keyword, start)) >= 0:
                if not _is_negated(text, index):
                    return True
                start = index + len(keyword)
        return any(
            not _is_negated(text, match.start())
            for pattern in self._patterns
            for match in pattern.finditer(text)
        )


class EmergencyRuleEngine:
    """急症规则引擎，从 YAML 配置文件加载规则并提供匹配能力。

    提供两个核心方法：
    - precheck_text：文字前置预判（不调外部服务）
    - evaluate：综合评估（文字 + 图片 + 档案）
    """

    def __init__(self, config_path: Path | None = None, settings: Settings | None = None):
        self._config_path = config_path or CONFIG_DIR / "emergency_rules.yaml"
        self._rules: list[EmergencyRule] = []
        self._settings = settings

    async def load(self) -> None:
        """从 YAML 配置文件加载急症规则。"""
        with open(self._config_path, encoding="utf-8") as f:
            raw_rules = yaml.safe_load(f) or []
        self._rules = [EmergencyRule(r) for r in raw_rules]
        logger.info("急症规则加载完成: %d 条", len(self._rules))

    # ------------------------------------------------------------ 前置预判

    def precheck_text(
        self,
        *,
        text: str,
        pet_info: PetInfo | None = None,
    ) -> EmergencyResult:
        """流程最前的纯规则预判（v6.3 §4 步骤 3）。

        只查文字+档案，不调外部服务。
        命中后记录强制急症标记；后续任何失败都走固定急症模板。

        Args:
            text: 用户输入的文本
            pet_info: 宠物档案（用于脆弱判断）

        Returns:
            EmergencyResult 包含风险等级和建议行动
        """
        matched = [r for r in self._rules if r.search(text or "")]
        return self._build_result(matched, reasons=[r.id for r in matched], pet_info=pet_info)

    # ------------------------------------------------------------ 最终评估

    def evaluate(
        self,
        *,
        text: str,
        red_flags: list[str] | None = None,
        pet_info: PetInfo | None = None,
        vision_findings: list[VisionFinding] | None = None,
        precheck: EmergencyResult | None = None,
    ) -> EmergencyResult:
        """最终风险分级（v6.3 §4 步骤 11）。

        合并文字预判、图片红旗、宠物档案做最终分级。
        脆弱宠物（幼/老）自动上调风险等级。

        Args:
            text: 用户输入文本
            red_flags: 图片分析中的危险信号
            pet_info: 宠物档案
            vision_findings: 视觉分析发现
            precheck: 前置预判结果

        Returns:
            综合评估后的 EmergencyResult
        """
        combined = f"{text or ''} {' '.join(red_flags or [])}"
        matched: list[EmergencyRule] = []
        reasons: list[str] = []
        if precheck is not None:
            matched.extend(r for r in self._rules if r.id in precheck.matched_rule_ids)
            reasons.extend(precheck.reasons)
        if combined.strip():
            for rule in self._rules:
                if rule.id not in reasons and rule.search(combined):
                    matched.append(rule)
                    reasons.append(rule.id)

        # 图片 red_flags 关键词兜底（保守：观察到的危险迹象按 high 起）
        flag_hits = [
            flag for flag in (red_flags or []) if any(kw in flag for kw in _RED_FLAG_KEYWORDS)
        ]
        for flag in flag_hits:
            reasons.append(f"red_flag:{flag[:40]}")
        if flag_hits:
            matched.append(self._keyword_rule(RiskLevel.HIGH, VetUrgency.URGENT))

        return self._build_result(matched, reasons=reasons, pet_info=pet_info)

    # ------------------------------------------------------------ 内部

    def _keyword_rule(self, level: RiskLevel, urgency: VetUrgency) -> EmergencyRule:
        """为图片 red_flag 关键词兜底创建临时规则。"""
        return EmergencyRule(
            {
                "id": "red_flag_fallback",
                "level": level.value,
                "vet_urgency": urgency.value,
                "force_urgent_guidance": True,
                "immediate_actions": ["尽快就医排查原因"],
                "avoid_actions": ["不要自行处理"],
            }
        )

    def _build_result(
        self,
        matched: list[EmergencyRule],
        *,
        reasons: list[str],
        pet_info: PetInfo | None,
    ) -> EmergencyResult:
        """根据匹配的规则构建最终评估结果。

        处理逻辑：
        1. 无匹配 → 脆弱宠物默认 MEDIUM，否则 LOW
        2. 有匹配 → 取最高级别，脆弱宠物上调一级
        3. 合并所有匹配规则的行动建议（去重）
        """
        if not matched:
            level = RiskLevel.MEDIUM if _is_vulnerable(pet_info) else RiskLevel.LOW
            return EmergencyResult(force_urgent_guidance=False, level=level, reasons=reasons)

        level = max((r.level for r in matched), key=lambda x: RISK_ORDER.index(x))
        urgency = max((r.vet_urgency for r in matched), key=lambda x: VET_URGENCY_ORDER.index(x))
        if _is_vulnerable(pet_info):
            level = _bump_risk(level)
            urgency = _bump_urgency(urgency)
        immediate = _dedup(a for r in matched for a in r.immediate_actions)
        avoid = _dedup(a for r in matched for a in r.avoid_actions)
        return EmergencyResult(
            force_urgent_guidance=any(r.force_urgent_guidance for r in matched),
            level=level,
            vet_urgency=urgency,
            matched_rule_ids=[r.id for r in matched],
            reasons=reasons,
            immediate_actions=immediate,
            avoid_actions=avoid,
        )

    def hints(self, matched_rule_ids: list[str]) -> list[str]:
        """获取命中规则的急症说明（固定急症模板用）。"""
        return [
            r.id for r in self._rules if r.id in matched_rule_ids
        ]


# ------------------------------------------------------------ 工具函数

def _is_vulnerable(pet_info: PetInfo | None) -> bool:
    """判断宠物是否为脆弱群体（幼宠 3 月龄以下或老年宠 10 岁以上）。"""
    if not pet_info:
        return False
    age = pet_info.age_months
    if age is not None:
        if age <= 3 or age >= 120:
            return True
    return bool(pet_info.species and any(k in pet_info.species for k in ("幼", "老年", "老")))


def _bump_risk(level: RiskLevel) -> RiskLevel:
    """风险等级上调一级。"""
    idx = min(RISK_ORDER.index(level) + 1, len(RISK_ORDER) - 1)
    return RISK_ORDER[idx]


def _bump_urgency(urgency: VetUrgency) -> VetUrgency:
    """就医紧迫度上调一级。"""
    idx = min(VET_URGENCY_ORDER.index(urgency) + 1, len(VET_URGENCY_ORDER) - 1)
    return VET_URGENCY_ORDER[idx]


def _dedup(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out