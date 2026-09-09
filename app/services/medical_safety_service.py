"""
医疗安全服务（v6.3 §13.3 / §13.5）

处理流程：
1. review：检查生成结果是否符合安全规则
2. 不合格 → repair_locally（确定性修复）→ 重写一次（rewrite_once）
3. 仍不合格 → build_fixed_safe_answer（固定安全回答模板）
4. 生成服务不可用且命中急症 → build_fixed_urgent_answer（固定急症指导模板）

额外处理：
- soften_diagnosis_language：确定性软化（"是XX病" → "可能是XX病"）
- clean_owner_facing_language：清理混合语言、不可靠判断、跨主题建议
"""
from __future__ import annotations

import logging
import re

from app.agent.state import ConsultState
from app.core.constants import (
    DEFAULT_DISCLAIMER,
    RISK_ORDER,
    VET_URGENCY_ORDER,
    AnswerMode,
    RiskLevel,
    VetUrgency,
)
from app.prompts.fixed_safe_answers import build_fixed_safe_answer, build_fixed_urgent_answer
from app.safety.diagnosis_rules import soften_diagnosis_assertions
from app.safety.medical_checker import MedicalSafetyChecker
from app.schemas.consult import GeneratedConsultation
from app.schemas.safety import MedicalReviewResult

logger = logging.getLogger(__name__)


class MedicalSafetyService:
    """医疗安全服务，负责问诊建议的安全检查、修复和兜底回答生成。

    职责链：
    1. review：调用 MedicalSafetyChecker 检查安全合规
    2. soften_diagnosis_language：确定性软化确诊式断言
    3. clean_owner_facing_language：清理混合语言/不可靠判断
    4. repair_locally：可确定性修复的问题做字段级修复
    5. rewrite_once：调用 LLM 重写一次不合格回答
    6. build_fixed_safe_answer / build_fixed_urgent_answer：兜底模板
    """

    def __init__(self, settings, checker: MedicalSafetyChecker):
        self.s = settings
        self.checker = checker

    async def review(
        self,
        generated: GeneratedConsultation,
        *,
        red_flags: list[str] | None = None,
        expected_risk: RiskLevel = RiskLevel.LOW,
        expected_urgency: VetUrgency = VetUrgency.NONE,
    ) -> MedicalReviewResult:
        """对生成结果进行医疗安全合规检查。

        委托给 MedicalSafetyChecker.review 执行四项检查：
        1. 药品安全违规
        2. 确诊式断言违规
        3. 眼部不安全家庭操作
        4. 就医建议是否与风险等级匹配

        Args:
            generated: 待检查的生成结果
            red_flags: 危险症状红旗列表
            expected_risk: 上游风险评估的预期风险等级
            expected_urgency: 上游风险评估的预期紧急程度

        Returns:
            检查结果，包含违规项列表和处置建议
        """
        return self.checker.review(
            generated,
            red_flags=red_flags,
            expected_risk=expected_risk,
            expected_urgency=expected_urgency,
        )

    @staticmethod
    def soften_diagnosis_language(
        generated: GeneratedConsultation,
    ) -> GeneratedConsultation:
        """确定性软化确诊/保证式措辞。

        在不改变原始回答结构的前提下，使用正则规则将明确的确诊式断言
        （如"是XX病"）替换为不确定性表述（如"可能是XX病"）。

        适用范围：answer_text、summary、disclaimer 等所有文本字段。
        不会修改结构化数据字段（如风险等级、催诊紧急程度）。
        """
        payload = generated.model_dump(mode="python")

        def transform(value):
            if isinstance(value, str):
                return soften_diagnosis_assertions(value)
            if isinstance(value, list):
                return [transform(item) for item in value]
            if isinstance(value, dict):
                return {key: transform(item) for key, item in value.items()}
            return value

        return GeneratedConsultation.model_validate(transform(payload))

    @staticmethod
    def clean_owner_facing_language(
        generated: GeneratedConsultation,
        *,
        is_eye_case: bool = False,
        user_text: str = "",
        rag_categories: tuple[str, ...] = (),
    ) -> tuple[GeneratedConsultation, bool]:
        """清理面向宠主的语言表述，确保清晰、安全、一致。

        执行以下清理：
        1. 混合语言清理：英文术语 → 中文（lethargy → 精神萎靡等）
        2. 不可靠判断清理：耳尖/脚垫判断发热、颈部皮肤回弹判断脱水、禁食建议
        3. 眼部非必需操作清理：非眼部问题中删除擦拭眼周建议
        4. 口腔问题跨主题清理：非用户提及的洗澡建议
        5. 跨列表精确去重：相同模板文字只保留一次
        6. A3 腹泻问题首句承接：以"猫咪/狗狗目前出现了腹泻"开头

        v7.2 改为字段感知处理：处置类替换只进入处置/观察字段，
        避免递归把 summary、病因等整体替换成同一句护理模板。

        Returns:
            (清理后的结果, 是否发生了变更)
        """
        payload = generated.model_dump(mode="python")
        action_fields = {"what_to_do_now", "avoid_actions", "what_to_monitor"}
        list_fields = (
            "visible_findings",
            "possible_explanations",
            "what_to_do_now",
            "avoid_actions",
            "what_to_monitor",
            "follow_up_questions",
        )
        normalized_user = re.sub(r"\s+", "", user_text.lower())
        is_oral_case = (
            "oral" in rag_categories
            and any(term in normalized_user for term in ("口臭", "嘴臭", "口腔", "牙龈", "牙齿"))
        )
        bathing_is_user_topic = any(
            term in normalized_user for term in ("洗澡", "洗护", "能洗", "可以洗")
        )

        def clean_text(value: str, field: str) -> str:
            cleaned = re.sub(r"\blethargy\b", "精神萎靡", value, flags=re.IGNORECASE)
            cleaned = re.sub(r"\bappetite\b", "食欲", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\bvomiting\b", "呕吐", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\bdiarrhea\b", "腹泻", cleaned, flags=re.IGNORECASE)

            if (
                ("耳尖" in cleaned or "耳朵" in cleaned or "脚垫" in cleaned)
                and any(term in cleaned for term in ("发烧", "发热", "体温", "温度过高"))
            ):
                cleaned = (
                    "如需判断是否发热，请使用宠物适用体温计并按兽医指导规范测量；"
                    "不要只凭耳朵或脚垫温度判断。"
                    if field in action_fields
                    else "仅凭耳朵或脚垫温度不能可靠判断是否发热。"
                )
            if (
                "颈部皮肤" in cleaned
                and ("回弹" in cleaned or "2 秒" in cleaned or "2秒" in cleaned)
            ):
                cleaned = (
                    "观察饮水、尿量和牙龈是否明显干黏；若怀疑脱水，请联系兽医评估。"
                    if field in action_fields
                    else "不能只凭颈部皮肤回弹时间确定是否脱水。"
                )
            if (
                any(term in cleaned for term in ("禁食", "停止喂食", "暂停喂食"))
                and any(term in cleaned for term in ("小时", "半天", "一天", "24"))
            ):
                cleaned = (
                    "若宠物愿意进食，可少量多次提供平时耐受、易消化的食物；"
                    "幼龄、老年或有基础病的宠物不要自行禁食。"
                    if field in action_fields
                    else "不建议宠物自行采用固定时长禁食。"
                )
            return cleaned.strip()

        cleaned_payload = dict(payload)
        for field, value in payload.items():
            if isinstance(value, str):
                cleaned_payload[field] = clean_text(value, field)
            elif isinstance(value, list):
                items: list = []
                for item in value:
                    if not isinstance(item, str):
                        items.append(item)
                        continue
                    cleaned = clean_text(item, field)
                    if (
                        not is_eye_case
                        and any(term in cleaned for term in ("擦拭眼周", "清洁眼周", "眼周附近"))
                    ):
                        cleaned = re.sub(
                            r"[^，。；]*?(?:擦拭|清洁)[^，。；]*眼周[^，。；]*(?:[，。；]|$)",
                            "",
                            cleaned,
                        ).lstrip("并且，； ")
                    if (
                        is_oral_case
                        and not bathing_is_user_topic
                        and any(term in cleaned for term in ("洗澡", "洗护", "沐浴"))
                    ):
                        continue
                    if cleaned:
                        items.append(cleaned)
                cleaned_payload[field] = items
            elif isinstance(value, dict):
                cleaned_payload[field] = {
                    key: clean_text(item, field) if isinstance(item, str) else item
                    for key, item in value.items()
                }

        # 渲染前跨列表精确去重。标点/空白不同但文字相同的模板只保留第一次。
        seen: set[str] = set()
        for field in list_fields:
            unique: list = []
            for item in cleaned_payload.get(field, []):
                if not isinstance(item, str):
                    unique.append(item)
                    continue
                canonical = re.sub(r"[\s，。；！？、,.!?]+", "", item.lower())
                if not canonical or canonical in seen:
                    continue
                seen.add(canonical)
                unique.append(item)
            cleaned_payload[field] = unique

        # A3：短腹泻问题首句先承接当前情况，而不是直接用护理指令开头。
        summary = str(cleaned_payload.get("summary", "")).strip()
        if any(term in normalized_user for term in ("拉稀", "腹泻", "软便")):
            starts_with_advice = bool(
                re.match(r"^(?:建议|可以|请|先|保持|避免|给予|提供|确保)", summary)
            )
            if starts_with_advice or not any(term in summary for term in ("拉稀", "腹泻", "软便")):
                pet_label = "猫咪" if "猫" in normalized_user else "狗狗" if "狗" in normalized_user else "宠物"
                cleaned_payload["summary"] = f"{pet_label}目前出现了腹泻或拉稀的情况。{summary}"

        changed = cleaned_payload != payload
        if changed:
            # 防止旧两段式正文绕过清理后的结构化字段。
            cleaned_payload["answer_text"] = ""
        return GeneratedConsultation.model_validate(cleaned_payload), changed

    def repair_locally(
        self,
        generated: GeneratedConsultation,
        *,
        violations: list[str],
        expected_risk: RiskLevel,
        expected_urgency: VetUrgency,
    ) -> GeneratedConsultation | None:
        """对可确定性修复的规则问题做字段级修改，不重新生成整段回答。

        可确定性修复的违规类型（无需模型重写）：
        - 确诊式断言 → 软化措辞
        - 缺少免责声明 → 补充默认免责声明
        - 处置建议矛盾 → 删除冲突项
        - 眼部不安全操作 → 删除相关条目
        - 风险等级/紧急程度不足 → 上调

        Returns:
            修复后的 GeneratedConsultation；返回 None 表示存在无法安全本地修复的问题，
            调用方需要走一次模型重写
        """
        repairable = {
            "出现确诊式断言",
            "把可能性写成确定事实",
            "给出'可以不用就医'类过度保证",
            "处置建议互相矛盾",
            "缺少免责声明",
            "回答风险等级低于上游风险",
            "就医紧急程度低于上游风险要求",
            "急症回答必须建议立即急诊",
            "就医建议缺失或紧急程度不足",
            "中风险但未建议就医或建议无紧急程度",
            "遗漏危险症状，未建议就医",
            "眼部不安全家庭操作建议",
        }
        if not violations or not set(violations).issubset(repairable):
            return None

        repaired = self.soften_diagnosis_language(generated)
        changed = repaired.model_dump(mode="python") != generated.model_dump(mode="python")

        if "缺少免责声明" in violations:
            repaired.disclaimer = DEFAULT_DISCLAIMER
            changed = True

        if "处置建议互相矛盾" in violations:
            actions = self._remove_conflicting_actions(repaired.what_to_do_now)
            changed = changed or actions != repaired.what_to_do_now
            repaired.what_to_do_now = actions

        if "眼部不安全家庭操作建议" in violations:
            actions = [
                item
                for item in repaired.what_to_do_now
                if not any(
                    term in item
                    for term in (
                        "翻眼皮", "翻开眼皮", "翻眼睑", "冲洗眼球", "冲洗眼睛",
                        "人用眼药", "棉签", "镊子", "眼药膏",
                    )
                )
            ]
            changed = changed or actions != repaired.what_to_do_now
            repaired.what_to_do_now = actions

        if RISK_ORDER.index(repaired.risk_level) < RISK_ORDER.index(expected_risk):
            repaired.risk_level = expected_risk
            changed = True

        required_urgency = expected_urgency
        if expected_risk == RiskLevel.EMERGENCY:
            required_urgency = VetUrgency.EMERGENCY
        elif expected_risk == RiskLevel.HIGH:
            required_urgency = max(
                required_urgency,
                VetUrgency.URGENT,
                key=VET_URGENCY_ORDER.index,
            )
        elif expected_risk == RiskLevel.MEDIUM:
            required_urgency = max(
                required_urgency,
                VetUrgency.BOOK_VET,
                key=VET_URGENCY_ORDER.index,
            )
        if "遗漏危险症状，未建议就医" in violations:
            required_urgency = max(
                required_urgency,
                VetUrgency.BOOK_VET,
                key=VET_URGENCY_ORDER.index,
            )
        if (
            VET_URGENCY_ORDER.index(repaired.vet_recommendation.urgency)
            < VET_URGENCY_ORDER.index(required_urgency)
        ):
            repaired.vet_recommendation.urgency = required_urgency
            changed = True
        if required_urgency != VetUrgency.NONE and not repaired.vet_recommendation.recommended:
            repaired.vet_recommendation.recommended = True
            changed = True
        if changed and not repaired.vet_recommendation.reason and required_urgency != VetUrgency.NONE:
            repaired.vet_recommendation.reason = "请结合当前风险等级及时联系兽医检查。"

        # 原始正文可能仍含已修复前的违规句；清空后由保留的结构化字段重新渲染。
        if changed or violations:
            repaired.answer_text = ""
        return repaired

    @staticmethod
    def _remove_conflicting_actions(actions: list[str]) -> list[str]:
        """删除冲突的处置建议组合。

        检测以下冲突并删除风险更高的一侧：
        - 同时建议热敷和冷敷 → 删除两者
        - 同时建议禁食和继续喂食 → 删除禁食
        """
        has_hot = any("热敷" in item for item in actions)
        has_cold = any("冷敷" in item or "冰敷" in item for item in actions)
        has_fast = any(
            term in item
            for item in actions
            for term in ("禁食", "停止喂食", "暂停喂食")
        )
        has_feed = any(
            term in item
            for item in actions
            for term in ("继续喂食", "正常喂食", "多喂")
        )
        cleaned: list[str] = []
        for item in actions:
            if has_hot and has_cold and any(term in item for term in ("热敷", "冷敷", "冰敷")):
                continue
            if has_fast and has_feed and any(term in item for term in ("禁食", "停止喂食", "暂停喂食")):
                continue
            cleaned.append(item)
        return cleaned

    def build_fixed_safe_answer(self, state: ConsultState) -> GeneratedConsultation:
        """固定安全回答模板生成（v6.3 §13.4）。

        当医疗检查 + 重写后仍不合格时使用此兜底策略。
        不依赖生成模型，直接使用预定义的模板，根据风险等级和回答模式
        渲染安全、合规的固定回答。

        如果风险等级为 EMERGENCY，自动升级为急症指导模板。
        """
        risk_level = (
            state.risk_result.level
            if state.risk_result
            else state.emergency_result.level
            if state.emergency_result
            else state.text_emergency_precheck.level
            if state.text_emergency_precheck
            else RiskLevel.MEDIUM
        )
        vet_urgency = (
            state.risk_result.vet_urgency
            if state.risk_result
            else state.emergency_result.vet_urgency
            if state.emergency_result
            else state.text_emergency_precheck.vet_urgency
            if state.text_emergency_precheck
            else VetUrgency.BOOK_VET
        )
        if risk_level == RiskLevel.EMERGENCY:
            return self.build_fixed_urgent_answer(state)
        answer_mode = state.generated.answer_mode if state.generated else AnswerMode.NORMAL
        return build_fixed_safe_answer(
            risk_level=risk_level,
            vet_urgency=vet_urgency,
            answer_mode=answer_mode,
            case_facts=state.case_facts,
            follow_up_questions=(
                state.completeness.questions if state.completeness else None
            ),
        )

    def build_fixed_urgent_answer(self, state: ConsultState) -> GeneratedConsultation:
        """固定急症指导模板生成（v6.3 §13.5 六要素）。

        在以下场景使用：
        1. Guard 内容审核失败提前短路时（emergency_result 尚未计算）
        2. 生成服务不可用且命中急症时
        3. 固定安全回答检测到风险等级为 EMERGENCY 时

        六要素：原因说明、立即行动、避免行动、兽医紧急程度、风险等级、免责声明。

        Note:
            V1.1 P1-1：Guard 失败提前短路时 emergency_result 尚未计算，
            用 text_emergency_precheck（纯规则 precheck 结果）兜底。
        """
        er = state.emergency_result or state.text_emergency_precheck
        return build_fixed_urgent_answer(
            reasons=er.reasons if er else [],
            immediate_actions=er.immediate_actions if er else [],
            avoid_actions=er.avoid_actions if er else [],
            vet_urgency=er.vet_urgency if er else VetUrgency.URGENT,
            risk_level=er.level if er else RiskLevel.HIGH,
        )

    async def close(self) -> None:  # pragma: no cover
        pass