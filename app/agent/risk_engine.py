"""风险聚合引擎（v6.3 §4 步骤 11：合并文字预判、图片红旗、病例档案）。

【核心定位】
RiskEngine 是问诊流水线 COMPLETENESS_AND_RISK 阶段的最后一环，负责把三路
独立的风险信号聚合成一个最终风险结论（RiskResult）：
    1. 急症规则引擎结果（EmergencyRuleEngine，文字 + 图片 red_flags + 档案）
    2. 图片质量信号（VisionFinding.image_quality）
    3. 多轮病例事实（FollowUpTracker 抽取的 CaseFacts，目前第一期覆盖眼部）

【聚合原则：只升不降】
- 任何一路信号给出更高风险，最终结果就取最高值（max_level / max_urgency）
- 风险等级（RiskLevel）：LOW < MEDIUM < HIGH < EMERGENCY
- 就医紧急度（VetUrgency）：NONE < MONITOR < BOOK_VET < WITHIN_24_HOURS < URGENT < EMERGENCY
- 每一次升档都会在 reasons 中追加机器可读原因码，便于存档分析与前端展示

【为什么单独成引擎？】
- 急症规则（emergency_rules）是通用规则匹配；图片质量、眼部病例事实是
  本系统特有的确定性信号，放在一起会让规则引擎膨胀。
- RiskEngine 只做"聚合 + 专科升档"，保持每一路信号的判定逻辑独立可测。

【典型升档场景（眼部专科）】
- 脓性/黄绿色分泌物        → MEDIUM + BOOK_VET
- 眯眼/抓挠/畏光/疼痛      → MEDIUM + BOOK_VET
- 脓性分泌物 + 不适表现    → 紧急度升到 WITHIN_24_HOURS（24 小时内就医）
- 眼球突出/突然失明/出血/外伤 → HIGH + URGENT（尽快就医）
- 症状持续超过 48 小时      → MEDIUM + BOOK_VET
"""
from __future__ import annotations

import logging

from app.agent.state import ConsultState
from app.core.config import Settings
from app.core.constants import RISK_ORDER, RiskLevel, VetUrgency, VET_URGENCY_ORDER
from app.safety.emergency_rules import EmergencyRuleEngine
from app.schemas.safety import RiskResult

logger = logging.getLogger(__name__)


class RiskEngine:
    """最终风险聚合器：急症规则、图片质量、多轮病例事实三路信号取最高。

    【依赖】
    - settings：应用配置（预留，当前聚合规则为确定性内置逻辑）
    - emergency_rules：急症规则引擎，用于在 state.emergency_result 缺失时
      兜底补算（正常流程中 _execute() 已先调用 emergency_rules.evaluate()）

    【输出】
    RiskResult(level, vet_urgency, reasons)，写入 state.risk_result，
    下游用于：
    - 决定生成模式（HIGH → urgent_guidance 急症模板）
    - EMERGENCY 直接短路固定急症模板
    - 医疗审核的 expected_risk / expected_urgency 基线
    - 最终响应的 risk_level 与就医建议
    """

    def __init__(self, settings: Settings, emergency_rules: EmergencyRuleEngine):
        """初始化风险聚合引擎。

        :param settings: 应用配置（Settings）
        :param emergency_rules: 急症规则引擎实例（EmergencyRuleEngine）
        """
        self.s = settings
        self.emergency = emergency_rules

    def evaluate(self, state: ConsultState) -> RiskResult:
        """聚合最终风险等级与就医紧急度。

        【处理顺序】
        1. 取急症规则结果（state.emergency_result）；若上游未算则当场补算，
           输入为：聚合用户原文 + 所有图片发现的 red_flags + 宠物档案 + 文字预判
        2. 图片质量升档：LOW 风险但存在 poor 质量图片 → 升到 MEDIUM，
           因为模糊/遮挡图片意味着关键体征可能被漏判
        3. 眼部病例事实升档（CaseFacts.domain == "eye"）：
           - 危险体征（眼球突出/突然失明/出血/外伤）→ HIGH + URGENT
           - 脓性分泌物 / 不适表现 / 持续超 48h → MEDIUM + BOOK_VET
           - 脓性分泌物 + 不适同时出现 → 紧急度再升到 WITHIN_24_HOURS

        【设计要点】
        - 只升不降：所有调整都通过 max_level / max_urgency 取最大值
        - 原因可追溯：每次升档追加原因码（如 "eye_fact:danger_sign:突然失明"）
        - 原因去重：最终 reasons 经 dict.fromkeys 去重，保持顺序稳定

        :param state: 状态总线（读取 emergency_result/vision_findings/case_facts）
        :return: 聚合后的 RiskResult（level + vet_urgency + reasons）
        """
        # 1. 急症规则结果（主信号）。正常由 _execute() 预先算好写入 state；
        #    此处兜底补算，保证 RiskEngine 可独立调用/单测。
        result = state.emergency_result
        if result is None:
            result = self.emergency.evaluate(
                text=state.text,
                # 把所有图片发现中的 red_flags 展平成一维列表传给规则引擎
                red_flags=[f for f in state.vision_findings for f in f.red_flags],
                pet_info=state.pet_info,
                precheck=state.text_emergency_precheck,
            )
        reasons = list(result.reasons)

        # 2. 图片质量升档：
        #    图片质量差（poor）但仍可用 → 观察结论可靠性下降，风险下限提到 MEDIUM，
        #    同时给出"建议预约就医"的紧急度，防止仅凭模糊图片给出低风险安慰。
        level = result.level
        urgency = result.vet_urgency
        if level == RiskLevel.LOW and state.vision_findings:
            if any(f.image_quality == "poor" for f in state.vision_findings):
                level = RiskLevel.MEDIUM
                urgency = self.max_urgency(urgency, VetUrgency.BOOK_VET)
                reasons.append("image_quality:poor")

        # 3. 眼部专科病例事实升档（FollowUpTracker 从多轮文字 + 图片观察中抽取）
        facts = state.case_facts
        if facts.domain == "eye":
            # 3.1 脓性/黄绿色/黏稠分泌物：细菌性结膜炎/角膜问题风险，需就医
            purulent_discharge = any(
                term in f"{facts.discharge_color}{facts.discharge_character}"
                for term in ("黄", "绿", "脓", "黏稠")
            )
            # 3.2 眼部不适表现：眯眼、抓挠、畏光、疼痛、睁不开
            discomfort = any(
                sign in facts.eye_signs
                for sign in ("眯眼", "抓挠", "畏光", "疼痛", "睁不开")
            )
            # 3.3 危险体征集合：眼球突出/脱出、突然失明、眼部出血、眼部外伤
            #     这类体征可能涉及角膜溃疡穿孔、青光眼、眼球损伤，需尽快就医
            danger_signs = set(facts.eye_signs) & {
                "眼球突出",
                "突然失明",
                "眼部出血",
                "眼部外伤",
            }
            if danger_signs:
                level = self.max_level(level, RiskLevel.HIGH)
                urgency = self.max_urgency(urgency, VetUrgency.URGENT)
                reasons.append("eye_fact:danger_sign:" + ",".join(sorted(danger_signs)))
            if purulent_discharge:
                level = self.max_level(level, RiskLevel.MEDIUM)
                urgency = self.max_urgency(urgency, VetUrgency.BOOK_VET)
                reasons.append("eye_fact:purulent_discharge")
            if discomfort:
                level = self.max_level(level, RiskLevel.MEDIUM)
                urgency = self.max_urgency(urgency, VetUrgency.BOOK_VET)
                reasons.append("eye_fact:discomfort")
            # 3.4 持续时间：眼部异常超过 48 小时未缓解 → 不建议继续自行观察
            if facts.duration_hours is not None and facts.duration_hours > 48:
                level = self.max_level(level, RiskLevel.MEDIUM)
                urgency = self.max_urgency(urgency, VetUrgency.BOOK_VET)
                reasons.append("eye_fact:duration_over_48h")
            # 3.5 脓性分泌物 + 不适表现同时存在：炎症可能性高，
            #     紧急度从"预约就医"收紧到"24 小时内就医"
            if purulent_discharge and discomfort:
                urgency = self.max_urgency(urgency, VetUrgency.WITHIN_24_HOURS)

        return RiskResult(
            level=level,
            vet_urgency=urgency,
            # dict.fromkeys：按出现顺序去重（同一原因码不重复记录）
            reasons=list(dict.fromkeys(reasons)),
        )

    @staticmethod
    def max_level(*levels: RiskLevel) -> RiskLevel:
        """返回多个风险等级中的最高值。

        以 RISK_ORDER 列表中的下标作为严重度序关系
        （LOW < MEDIUM < HIGH < EMERGENCY）。

        :param levels: 一个或多个 RiskLevel
        :return: 其中严重度最高的等级
        """
        return max(levels, key=lambda x: RISK_ORDER.index(x))

    @staticmethod
    def max_urgency(*urgencies: VetUrgency) -> VetUrgency:
        """返回多个就医紧急度中的最高值。

        以 VET_URGENCY_ORDER 列表中的下标作为紧急度序关系
        （NONE < MONITOR < BOOK_VET < WITHIN_24_HOURS < URGENT < EMERGENCY）。

        :param urgencies: 一个或多个 VetUrgency
        :return: 其中最紧急的就医建议
        """
        return max(urgencies, key=lambda x: VET_URGENCY_ORDER.index(x))
