"""DeepSeek 官方 API 问诊封装。

- 业务层只依赖 KnowledgeConsultAdapter 抽象
- 调用失败抛 KnowledgeConsultUnavailable（agent 按急症/非急症降级）
"""
from __future__ import annotations

import logging

from app.agent.state import ConsultState
from app.clients.knowledge_consult_client import KnowledgeConsultAdapter, build_knowledge_consult_adapter
from app.core.config import Settings
from app.core.exceptions import KnowledgeConsultUnavailable
from app.schemas.consult import GeneratedConsultation, KnowledgeConsultRequest

logger = logging.getLogger(__name__)


class KnowledgeConsultService:
    def __init__(self, settings: Settings, adapter: KnowledgeConsultAdapter | None = None):
        self.s = settings
        self.adapter = adapter or build_knowledge_consult_adapter(settings)

    def build_request(self, state: ConsultState, answer_mode: str) -> KnowledgeConsultRequest:
        """统一内部请求（v6.3 §14.2：业务层不依赖 Provider 原始字段）。"""
        red_flags = [flag for f in state.vision_findings for flag in f.red_flags]
        limitations = [
            f"图片质量 {f.image_quality.value}"
            for f in state.vision_findings if f.image_quality.value != "good"
        ]
        return KnowledgeConsultRequest(
            user_question=state.text,
            pet_info=state.pet_info.model_dump() if state.pet_info else {},
            pets=[p.model_dump(exclude_none=True) for p in state.pets],
            active_pet_name=(
                state.pet_info.display_name if state.pet_info and len(state.pets) > 1 else None
            ),
            image_summary={
                "observations": [o for f in state.vision_findings for o in f.observations],
                "red_flags": red_flags,
                "limitations": limitations,
            },
            conversation_summary=state.history_summary or "",
            recent_turns=self._recent_turns(state),
            risk_context={
                "risk_level": state.risk_result.level.value if state.risk_result else "low",
                "vet_urgency": (
                    state.risk_result.vet_urgency.value if state.risk_result else "none"
                ),
                "matched_rules": state.emergency_result.matched_rule_ids if state.emergency_result else [],
                "risk_reasons": state.risk_result.reasons if state.risk_result else [],
            },
            case_facts=state.case_facts.model_dump(exclude_none=True),
            missing_information=state.completeness.questions if state.completeness else [],
            rag_evidence=state.rag_evidence,
            rag_decision=state.rag_result.decision.value if state.rag_result else "",
            answer_mode=answer_mode,
        )

    async def generate(
        self, state: ConsultState, answer_mode: str, *, deadline
    ) -> GeneratedConsultation:
        request = self.build_request(state, answer_mode)
        try:
            return await self.adapter.generate_consultation(
                request=request,
                deadline=deadline,
                request_id=state.request_id,
            )
        except KnowledgeConsultUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一映射为不可用
            raise KnowledgeConsultUnavailable(f"知识问诊调用失败: {exc}") from exc

    @staticmethod
    def _recent_turns(state: ConsultState) -> list[str]:
        lines: list[str] = []
        for t in state.history[-8:]:
            lines.append(f"用户补充: {t.user_text or '（仅上传图片）'}")
            lines.append(
                "系统状态: "
                f"risk={t.risk_level.value}, "
                f"mode={t.answer_mode.value if t.answer_mode else 'unknown'}, "
                f"urgency={t.vet_urgency.value}"
            )
        return lines

    async def close(self) -> None:
        await self.adapter.close()
