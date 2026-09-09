"""输出通用审核（v6.3 §13.1.1 场景化：pet_consult_output）

规则层检查结构化一致性（risk 与就医建议匹配）；
模型层（Qwen3Guard）结果由 moderation_service 结合 scene 判定。
医疗必要描述（伤口/出血）不因通用暴力标签无条件清空。
"""
from __future__ import annotations

import logging

from app.core.constants import RiskLevel, VetUrgency
from app.schemas.consult import GeneratedConsultation
from app.schemas.safety import ModerationResult

logger = logging.getLogger(__name__)


class OutputModerator:
    """规则层输出检查；返回 ModerationResult（requires_review=true 时 agent 转 review）。"""

    def check(self, generated: GeneratedConsultation) -> ModerationResult:
        # 结构性一致性：high/emergency 但 vet_recommendation 未建议就医 → 保守标记
        if generated.risk_level == RiskLevel.EMERGENCY:
            if (
                not generated.vet_recommendation.recommended
                or generated.vet_recommendation.urgency != VetUrgency.EMERGENCY
            ):
                return ModerationResult(
                    blocked=True,
                    verdict="Review",
                    categories=["emergency_urgency_mismatch"],
                    parse_ok=False,
                )
        elif generated.risk_level == RiskLevel.HIGH:
            if (
                not generated.vet_recommendation.recommended
                or generated.vet_recommendation.urgency
                not in (VetUrgency.URGENT, VetUrgency.EMERGENCY)
            ):
                return ModerationResult(
                    blocked=True,
                    verdict="Review",
                    categories=["risk_mismatch"],
                    parse_ok=False,
                )
        # answer_mode 与 risk 匹配性：urgent_guidance 必须带 emergency/urgent 就医建议
        if generated.answer_mode == "urgent_guidance":
            if generated.vet_recommendation.urgency not in ("urgent", "emergency"):
                return ModerationResult(
                    blocked=True,
                    verdict="Review",
                    categories=["mode_mismatch"],
                    parse_ok=False,
                )
        return ModerationResult(verdict="Safe", parse_ok=True)
