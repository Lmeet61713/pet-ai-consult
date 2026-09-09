"""安全相关结构（v6.3 §13.2 / §13.3）"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.constants import RiskLevel, VetUrgency


class ModerationResult(BaseModel):
    """通用内容审核结果（场景化：input/output）"""

    blocked: bool = False       # 是否阻断
    verdict: str = "Safe"          # Safe | Unsafe | Controversial | Review
    categories: list[str] = Field(default_factory=list)     # 分类
    raw: str = ""       # 原始文本
    parse_ok: bool = True       # 是否解析成功
    scene: str = ""       # 场景
    # v6.3 §13.1.1：医疗求助中的伤口/出血/车祸描述不因 Violent 标签直接拒绝，
    # 只有恶意越权/危险请求才拒绝
    should_refuse_medical_request: bool = False       # 是否拒绝医疗求助请求


class EmergencyResult(BaseModel):
    """急症规则引擎输出（v6.3 §13.2）"""

    force_urgent_guidance: bool = False
    level: RiskLevel = RiskLevel.LOW
    vet_urgency: VetUrgency = VetUrgency.NONE
    matched_rule_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    immediate_actions: list[str] = Field(default_factory=list)
    avoid_actions: list[str] = Field(default_factory=list)


class CompletenessResult(BaseModel):
    """信息完整度判断（v6.3：不阻断回答，只决定 provisional）

    reason 区分兜底来源（2026-08-19）：
      - hard_need  = 硬性兜底（无图无文/图片不可用/眼病缺失槽位等），必须走 provisional
      - keyword_thin = 关键词查缺（短文本信号不足），先初步回答并自然追问
    """

    need_more_info: bool = False
    questions: list[str] = Field(default_factory=list)
    reason: str | None = None


class RiskResult(BaseModel):
    """风险聚合输出"""

    level: RiskLevel = RiskLevel.LOW
    vet_urgency: VetUrgency = VetUrgency.NONE
    reasons: list[str] = Field(default_factory=list)


class MedicalReviewResult(BaseModel):
    """医疗安全后置检查输出（v6.3：recommended_action 含 fixed_safe_answer）"""

    passed: bool = True
    violations: list[str] = Field(default_factory=list)
    severity: str = "pass"         # pass | rewrite | fixed_safe_answer | review
    recommended_action: str = "pass"
