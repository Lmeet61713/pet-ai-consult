"""枚举与全局常量（v6.3 §7.1）"""
from __future__ import annotations

from enum import StrEnum


class ConsultStatus(StrEnum):
    """咨询状态"""
    SUCCESS = "success"
    REFUSE = "refuse"
    REVIEW = "review"
    ERROR = "error"


class AnswerMode(StrEnum):
    """回复模式"""
    NORMAL = "normal"
    PROVISIONAL = "provisional"
    URGENT_GUIDANCE = "urgent_guidance"


class VetUrgency(StrEnum):
    """兽医紧急程度"""
    NONE = "none"
    MONITOR = "monitor"
    BOOK_VET = "book_vet"
    WITHIN_24_HOURS = "within_24_hours"
    URGENT = "urgent"
    EMERGENCY = "emergency"


class RiskLevel(StrEnum):
    """风险等级"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EMERGENCY = "emergency"


class ImageQuality(StrEnum):
    """图片质量"""
    GOOD = "good"
    POOR = "poor"
    UNUSABLE = "unusable"


# 默认租户/用户（MVP 前端直连阶段；产品化后由 Token 提供）
DEFAULT_TENANT = "default"
DEFAULT_USER = "anon"

# 风险等级序（聚合取最高）
RISK_ORDER: tuple[RiskLevel, ...] = (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.EMERGENCY)

# 就医紧急程度序（聚合取最高）
VET_URGENCY_ORDER: tuple[VetUrgency, ...] = (
    VetUrgency.NONE, VetUrgency.MONITOR, VetUrgency.BOOK_VET,
    VetUrgency.WITHIN_24_HOURS, VetUrgency.URGENT, VetUrgency.EMERGENCY,
)

# 免责声明
DEFAULT_DISCLAIMER = "本回答仅用于初步信息参考，不能替代执业兽医检查。"

# Guard 场景（v6.3 §13.1.1 场景化审核）
GUARD_SCENE_INPUT = "pet_consult_input"
GUARD_SCENE_OUTPUT = "pet_consult_output"
