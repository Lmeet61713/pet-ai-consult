"""固定安全回答模板（v6.3 §13.5 / §13.4）

生成服务不可用或生成结果未通过医疗安全检查时，返回不依赖生成模型的固定指导。
固定急症模板 6 要素：风险原因 / 就医紧急程度 / 低风险措施 / 禁止事项 / 运输注意 / 恶化信号。
"""
from __future__ import annotations

from app.core.constants import (
    AnswerMode,
    RISK_ORDER,
    RiskLevel,
    VET_URGENCY_ORDER,
    VetUrgency,
)
from app.schemas.consult import GeneratedConsultation, VetRecommendation
from app.schemas.followup import CaseFacts

_DISCLAIMER = "本回答仅用于初步信息参考，不能替代执业兽医检查。"

def _max_risk(first: RiskLevel, second: RiskLevel) -> RiskLevel:
    return first if RISK_ORDER.index(first) >= RISK_ORDER.index(second) else second


def _max_urgency(first: VetUrgency, second: VetUrgency) -> VetUrgency:
    return first if VET_URGENCY_ORDER.index(first) >= VET_URGENCY_ORDER.index(second) else second


def build_fixed_safe_answer(
    *,
    risk_level: RiskLevel = RiskLevel.MEDIUM,
    vet_urgency: VetUrgency = VetUrgency.BOOK_VET,
    answer_mode: AnswerMode = AnswerMode.NORMAL,
    case_facts: CaseFacts | None = None,
    follow_up_questions: list[str] | None = None,
) -> GeneratedConsultation:
    """构造不会降低上游风险的固定兜底回答。"""
    # 临床风险来自上游规则；生成结果未通过审核不等于病例本身升为中风险。
    # 兜底内容仍保持保守，但不能覆盖已经确定的 LOW 风险结论。
    effective_risk = risk_level
    minimum_urgency = {
        RiskLevel.LOW: VetUrgency.NONE,
        RiskLevel.MEDIUM: VetUrgency.BOOK_VET,
        RiskLevel.HIGH: VetUrgency.URGENT,
        RiskLevel.EMERGENCY: VetUrgency.EMERGENCY,
    }[effective_risk]
    effective_urgency = _max_urgency(vet_urgency, minimum_urgency)
    effective_mode = (
        AnswerMode.URGENT_GUIDANCE
        if effective_risk in (RiskLevel.HIGH, RiskLevel.EMERGENCY)
        else AnswerMode.PROVISIONAL
        if answer_mode == AnswerMode.PROVISIONAL
        else AnswerMode.NORMAL
    )
    if case_facts and case_facts.domain == "eye":
        eye_urgency = _max_urgency(effective_urgency, VetUrgency.BOOK_VET)
        facts_text = "、".join(case_facts.confirmed_facts) or "存在眼部异常"
        questions = (follow_up_questions or [])[:3] if effective_mode == AnswerMode.PROVISIONAL else []
        return GeneratedConsultation(
            summary=(
                f"目前已知：{facts_text}。这些表现需要排查眼表刺激、炎症或角膜损伤，"
                "在线无法确认具体原因。"
            ),
            visible_findings=list(case_facts.confirmed_facts),
            possible_explanations=["眼表刺激或炎症", "需要线下检查排除角膜损伤"],
            what_to_do_now=[
                "只用干净湿纱布轻擦眼周分泌物，避免接触眼球",
                "防止宠物继续抓挠眼睛，必要时佩戴合适的伊丽莎白圈",
                "保持环境清洁，记录分泌物、眯眼和疼痛是否加重",
            ],
            avoid_actions=[
                "不要用棉签或镊子移除眼部异物，也不要自行翻眼皮",
                "不要自行冲洗或操作眼球",
                "不要使用人用眼药，也不要未经兽医建议使用抗生素或药膏",
            ],
            what_to_monitor=[
                "是否持续眯眼、睁不开或明显疼痛",
                "分泌物是否增多或呈黄绿色、脓性",
                "是否出现眼球浑浊、外伤、出血或视力异常",
            ],
            follow_up_questions=questions,
            risk_level=effective_risk,
            answer_mode=effective_mode,
            self_reported_confidence=None,
            vet_recommendation=VetRecommendation(
                recommended=True,
                urgency=eye_urgency,
                reason="眼部异常可能需要染色和眼科检查，请按当前紧急程度就医",
            ),
            disclaimer=_DISCLAIMER,
        )
    low_risk_fallback = effective_risk == RiskLevel.LOW
    return GeneratedConsultation(
        summary=(
            "根据目前提供的信息，暂未发现明确的紧急风险信号。"
            "当前信息仍比较有限，需要结合症状持续时间、频率和精神食欲继续判断。"
            if low_risk_fallback
            else "很抱歉，我暂时无法就这个情况给出可靠的在线建议。"
        ),
        visible_findings=[],
        possible_explanations=[],
        what_to_do_now=["观察宠物精神、食欲和呼吸状态", "保持正常饮水和舒适环境"],
        avoid_actions=["不要自行用药或喂食药物", "不要进行没有把握的家庭处置"],
        what_to_monitor=["症状是否加重", "精神状态是否变差", "食欲饮水是否正常"],
        follow_up_questions=(
            (follow_up_questions or [])[:3]
            if follow_up_questions is not None
            else
            [
                "症状从什么时候开始，是否在加重？",
                "宠物的精神、食欲和饮水情况如何？",
                "是否出现呼吸困难、持续出血或抽搐？",
            ]
            if effective_mode == AnswerMode.PROVISIONAL
            else []
        ),
        risk_level=effective_risk,
        answer_mode=effective_mode,
        self_reported_confidence=None,
        vet_recommendation=VetRecommendation(
            recommended=effective_urgency != VetUrgency.NONE,
            urgency=effective_urgency,
            reason=(
                "当前未发现需要立即就医的风险信号；如出现异常或状态变差，请联系兽医"
                if effective_urgency == VetUrgency.NONE
                else "无法可靠评估，请按当前风险等级尽快线下就医"
            ),
        ),
        disclaimer=_DISCLAIMER,
    )


FIXED_SAFE_ANSWER = build_fixed_safe_answer()


def build_fixed_urgent_answer(
    *,
    reasons: list[str],
    immediate_actions: list[str],
    avoid_actions: list[str],
    vet_urgency: VetUrgency,
    risk_level: RiskLevel,
) -> GeneratedConsultation:
    """固定急症指导（v6.3 §13.5 六要素；不依赖生成模型）。"""
    if risk_level == RiskLevel.EMERGENCY:
        vet_urgency = VetUrgency.EMERGENCY
    elif risk_level == RiskLevel.HIGH:
        vet_urgency = _max_urgency(vet_urgency, VetUrgency.URGENT)
    urgency_text = {
        VetUrgency.EMERGENCY: "请立即前往最近的宠物医院急诊",
        VetUrgency.URGENT: "请尽快（今天内）前往宠物医院",
        VetUrgency.WITHIN_24_HOURS: "请 24 小时内前往宠物医院",
    }.get(vet_urgency, "请尽快前往宠物医院")

    summary = "检测到高风险/急症情况：" + "、".join(reasons[:5]) + "。" + urgency_text + "。"
    what_to_do = list(immediate_actions or ["保持宠物安静，减少搬动", "尽快联系宠物医院"])
    what_to_do.append(urgency_text)
    if vet_urgency == VetUrgency.EMERGENCY:
        what_to_do.append("途中尽量平稳，使用通风的运输箱")
    what_to_monitor = [
        "呼吸是否更急促或出现张口呼吸",
        "精神状态是否持续变差",
        "是否出现新的出血或抽搐",
    ]
    avoid = list(avoid_actions or ["不要自行催吐", "不要强行喂食喂水喂药", "不要摇晃拍打宠物"])

    return GeneratedConsultation(
        summary=summary,
        visible_findings=[],
        possible_explanations=[],
        what_to_do_now=what_to_do,
        avoid_actions=avoid,
        what_to_monitor=what_to_monitor,
        follow_up_questions=[],
        risk_level=risk_level,
        answer_mode=AnswerMode.URGENT_GUIDANCE,
        self_reported_confidence=None,
        vet_recommendation=VetRecommendation(
            recommended=True,
            urgency=vet_urgency,
            reason="已命中急症规则" + ("；".join(reasons[:3])),
        ),
        disclaimer=_DISCLAIMER,
    )
