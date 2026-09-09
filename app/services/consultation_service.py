"""
问诊生成服务（v6.3 §9.2）

三种生成模式，由 ConsultAgent 根据风险评估结果选择：
- NORMAL：正常模式，完整可能性分析 + 护理观察 + 就医阈值
- PROVISIONAL：信息不足模式，初步回答 + 追问 + 补拍建议（Vision 不可用时降级）
- URGENT_GUIDANCE：急症指导模式，风险原因 + 立即行动 + 禁止事项 + 就医紧急程度

安全重写（rewrite_once）是医疗审核不通过时的补救机制，只允许 1 次。
"""
from __future__ import annotations

import logging

from app.agent.state import ConsultState
from app.core.constants import AnswerMode
from app.schemas.consult import GeneratedConsultation
from app.services.knowledge_consult_service import KnowledgeConsultService

logger = logging.getLogger(__name__)


class ConsultationService:
    """问诊建议生成服务，封装三种生成模式和安全重写逻辑。

    使用方式：
    1. ConsultAgent 根据风险评估结果选择模式（NORMAL/PROVISIONAL/URGENT_GUIDANCE）
    2. 生成结果经 MedicalSafetyService 检查
    3. 不合格时调用 rewrite_once 重写（仅 1 次机会）
    4. 仍不合格则降级为固定安全模板
    """

    def __init__(self, settings, knowledge_consult: KnowledgeConsultService):
        self.s = settings
        self.knowledge_consult = knowledge_consult  # 底层知识问诊 API 客户端

    async def generate(
        self, state: ConsultState, *, deadline
    ) -> GeneratedConsultation:
        """正常模式（NORMAL）：可能性分析 + 护理观察 + 就医阈值。

        标准问诊流程，包含：
        - 基于 RAG 检索结果的可能性分析
        - 家庭护理和观察建议
        - 明确就医阈值条件
        """
        return await self.knowledge_consult.generate(
            state, AnswerMode.NORMAL.value, deadline=deadline
        )

    async def generate_provisional(
        self, state: ConsultState, *, deadline
    ) -> GeneratedConsultation:
        """信息不足模式（PROVISIONAL）：初步回答 + 追问 + 补拍建议。

        当 Vision 视觉分析不可用或图片不足时使用此模式降级。
        不包含图片分析结果，但保留 RAG 检索和文本分析能力。
        """
        return await self.knowledge_consult.generate(
            state, AnswerMode.PROVISIONAL.value, deadline=deadline
        )

    async def generate_urgent_guidance(
        self, state: ConsultState, *, deadline
    ) -> GeneratedConsultation:
        """急症指导模式（URGENT_GUIDANCE）：高风险/急症专用。

        输出包含：风险原因 + 立即行动 + 禁止事项 + 就医紧急程度。
        由 EmergencyRuleEngine 评估结果触发，不依赖生成模型的完整分析能力。
        """
        return await self.knowledge_consult.generate(
            state, AnswerMode.URGENT_GUIDANCE.value, deadline=deadline
        )

    async def rewrite_once(
        self,
        state: ConsultState,
        previous: GeneratedConsultation,
        violations: list[str],
        *,
        deadline,
    ) -> GeneratedConsultation:
        """安全重写一次（v6.3 §16.2）。

        医疗审核发现违规时调用，只允许 1 次重写机会。
        如果重写失败或超时，直接降级为固定安全模板。

        V1.1 P1-2：携带上一版违规清单，adapter 注入"仅修正违规"约束，
        不再是同 prompt 无差别重生成。

        Args:
            state: 当前请求状态
            previous: 上一版生成的问诊建议（含违规内容）
            violations: 需要修正的违规项列表
            deadline: 剩余预算时间

        Returns:
            修正后的 GeneratedConsultation

        Raises:
            Exception: 重写失败时向上抛出，由调用方处理降级逻辑
        """
        request = self.knowledge_consult.build_request(
            state, previous.answer_mode.value if previous.answer_mode else AnswerMode.NORMAL.value
        )
        request = request.model_copy(
            update={
                "rewrite_violations": list(violations),
                "rewrite_source": previous.model_dump(mode="json"),
            }
        )
        try:
            return await self.knowledge_consult.adapter.generate_consultation(
                request=request,
                deadline=deadline,
                request_id=state.request_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("安全重写失败: %s", exc)
            raise

    async def close(self) -> None:
        """关闭底层知识问诊客户端连接。"""
        await self.knowledge_consult.close()