"""
医疗安全聚合检查（v5 §13.3）

组合 medication_rules + diagnosis_rules + 眼部操作检查。
命中严重问题 → recommended_action=rewrite；重写一次仍失败 → review/go_vet。

检查项：
1. 药品安全（剂量、禁忌、重复用药等）
2. 诊断确定性（禁止确诊式断言）
3. 眼部家庭操作（硬拦截，禁止盲目冲洗/翻眼皮等）
4. 就医建议与风险等级匹配（急症必须建议立即急诊）
"""
from __future__ import annotations

import logging
import re

from app.core.config import Settings
from app.core.constants import (
    RISK_ORDER,
    VET_URGENCY_ORDER,
    RiskLevel,
    VetUrgency,
)
from app.safety.diagnosis_rules import DiagnosisRules
from app.safety.medication_rules import MedicationRules
from app.schemas.consult import GeneratedConsultation
from app.schemas.safety import MedicalReviewResult

logger = logging.getLogger(__name__)


class MedicalSafetyChecker:
    """医疗安全聚合检查器。

    组合药品规则、诊断规则和眼部操作检查，确保生成结果安全合规。
    规则覆盖：药品安全、确诊式断言、眼部操作、就医建议匹配度。
    """

    def __init__(self, settings: Settings):
        self.s = settings
        self.medication = MedicationRules()  # 药品安全规则
        self.diagnosis = DiagnosisRules()  # 诊断确定性规则

    async def load(self) -> None:
        """异步加载药品规则（生产环境用）。"""
        await self.medication.load()

    def load_sync(self) -> None:
        """同步加载药品规则（测试用）。"""
        self.medication.load_sync()

    def review(
        self,
        generated: GeneratedConsultation,
        *,
        red_flags: list[str] | None = None,
        expected_risk: RiskLevel = RiskLevel.LOW,
        expected_urgency: VetUrgency = VetUrgency.NONE,
    ) -> MedicalReviewResult:
        """检查生成结果的安全合规性。

        检查项：
        1. 药品安全违规（剂量、禁忌、重复用药）
        2. 确诊式断言违规（"是XX病"类的确定性诊断）
        3. 眼部不安全家庭操作
        4. 就医建议是否与风险等级匹配

        Args:
            generated: 生成的问诊建议
            red_flags: 图片分析中的危险信号
            expected_risk: 上游风险评估的预期风险等级
            expected_urgency: 上游风险评估的预期就医紧迫度

        Returns:
            MedicalReviewResult: 通过/违规/建议动作
        """
        violations: list[str] = []

        text = _flatten(generated)
        violations.extend(self.medication.violations(text))
        violations.extend(self.diagnosis.violations(text))
        violations.extend(_eye_care_violations(text))

        # 就医建议与风险等级匹配（v6.3 §13.3：遗漏急症/级别不足都算违规）
        # 注意：不能用含免责声明的全文（免责声明必有"兽医"），只看建议正文
        vr = generated.vet_recommendation
        effective_risk = _max_risk(expected_risk, generated.risk_level)
        if RISK_ORDER.index(generated.risk_level) < RISK_ORDER.index(expected_risk):
            violations.append("回答风险等级低于上游风险")
        if VET_URGENCY_ORDER.index(vr.urgency) < VET_URGENCY_ORDER.index(expected_urgency):
            violations.append("就医紧急程度低于上游风险要求")

        if effective_risk == RiskLevel.EMERGENCY:
            if not vr.recommended or vr.urgency != VetUrgency.EMERGENCY:
                violations.append("急症回答必须建议立即急诊")
        elif effective_risk == RiskLevel.HIGH:
            if not vr.recommended or vr.urgency not in ("urgent", "emergency"):
                violations.append("就医建议缺失或紧急程度不足")
        elif effective_risk == RiskLevel.MEDIUM:
            if not vr.recommended or vr.urgency == VetUrgency.NONE:
                violations.append("中风险但未建议就医或建议无紧急程度")
        elif red_flags and vr.recommended is False:
            violations.append("遗漏危险症状，未建议就医")

        passed = not violations
        severity = "pass" if passed else "rewrite"
        return MedicalReviewResult(
            passed=passed,
            violations=violations,
            severity=severity,
            recommended_action=severity,
        )


def _max_risk(a: RiskLevel, b: RiskLevel) -> RiskLevel:
    """取两个风险等级中较高的一个。"""
    return a if RISK_ORDER.index(a) >= RISK_ORDER.index(b) else b


def _flatten(g: GeneratedConsultation) -> str:
    """将结构化问诊建议拼成纯文本，供正则规则检查（漏检兜底：全部字段）。"""
    parts = [
        g.answer_text,
        g.summary,
        g.vet_recommendation.reason,
        g.disclaimer,
    ]
    parts.extend(g.visible_findings)
    parts.extend(g.possible_explanations)
    parts.extend(g.what_to_do_now)
    parts.extend(g.avoid_actions)
    parts.extend(g.what_to_monitor)
    parts.extend(g.follow_up_questions)
    return "\n".join(p for p in parts if p)


# 眼部不安全家庭操作的正则模式集合
_EYE_OPERATION_PATTERNS = (
    re.compile(r"(?:棉签|镊子)[^，。；！？\n]{0,12}(?:取|移除|挑|夹|掏|清理)"),
    re.compile(r"(?:取|移除|挑|夹|掏)[^，。；！？\n]{0,12}(?:棉签|镊子)"),
    re.compile(r"翻(?:开)?(?:眼皮|眼睑)"),
    re.compile(r"(?:自行|轻轻|直接)?冲洗(?:眼球|眼睛|结膜囊)?"),
    re.compile(r"(?:使用|用|滴)[^，。；！？\n]{0,8}人用眼药"),
    re.compile(r"(?:使用|用|滴|涂|抹)[^，。；！？\n]{0,8}(?:抗生素|眼药膏|药膏)"),
)
# 否定表达识别（"不要冲洗眼睛"不误报）
_SAFETY_NEGATION_RE = re.compile(r"(?:不要|避免|禁止|切勿|不可|不应|不建议|不能|请勿)[^，。；！？\n]{0,16}$")


def _eye_care_violations(text: str) -> list[str]:
    """眼部家庭操作硬拦截检查。

    检查文本中是否包含不安全的眼部操作建议（如用棉签取异物、翻眼皮等），
    自动识别否定表达和兽医指导说明，避免误报。

    Returns:
        违规项列表，空列表表示无违规
    """
    if not any(term in text for term in ("眼", "眼球", "眼皮", "眼睑", "眼药")):
        return []
    violations: list[str] = []
    for segment in re.split(r"[，。；！？\n]", text):
        if not segment.strip():
            continue
        for pattern in _EYE_OPERATION_PATTERNS:
            for match in pattern.finditer(segment):
                prefix = segment[:match.start()]
                if _SAFETY_NEGATION_RE.search(prefix):
                    continue  # 否定表达，跳过
                if "兽医指导" in segment or "兽医建议" in segment or "兽医处方" in segment:
                    continue  # 在兽医指导下是安全的
                violations.append("眼部不安全家庭操作建议")
                break
            if violations:
                break
    return list(dict.fromkeys(violations))