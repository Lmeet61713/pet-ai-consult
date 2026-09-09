"""通用审核服务：规则层 + 模型层（Qwen3Guard GPU 服务）双审核（v6.3 §13.1.1）

场景化：pet_consult_input / pet_consult_output。
医疗求助中的"出血/伤口/车祸/误食"等描述即使命中 Violent 标签也不拒绝
（should_refuse_medical_request 仅对明确恶意请求置位）。
"""
from __future__ import annotations

import asyncio
import logging

from app.clients.guard_client import GuardClient
from app.core.config import Settings
from app.core.constants import GUARD_SCENE_INPUT, GUARD_SCENE_OUTPUT
from app.safety.input_moderator import InputModerator
from app.safety.output_moderator import OutputModerator
from app.schemas.consult import GeneratedConsultation
from app.schemas.safety import ModerationResult

logger = logging.getLogger(__name__)

_MEDICAL_VIOLENCE_CATEGORIES = frozenset(
    {"violent", "violence", "physical_violence", "physical_harm"}
)


def _is_medical_violence_only(result: ModerationResult, text: str) -> bool:
    """仅豁免医疗求助语境中的暴力/外伤标签，其他类别继续拦截。"""
    categories = {str(c).strip().lower() for c in result.categories if str(c).strip()}
    return bool(
        result.blocked
        and categories
        and categories <= _MEDICAL_VIOLENCE_CATEGORIES
        and InputModerator.is_medical_request(text)
    )


class ModerationService:
    def __init__(self, settings: Settings, guard: GuardClient):
        self.s = settings
        self.guard = guard
        self.input_rules = InputModerator()
        self.output_rules = OutputModerator()
        self._shadow_tasks: set[asyncio.Task[None]] = set()

    async def check_input(
        self, text: str, *, timeout_seconds: float | None = None, request_id: str = ""
    ) -> ModerationResult:
        """输入审核（scene=pet_consult_input）：规则先行；模型审核结合医疗豁免。"""
        blocked, reason = self.input_rules.check(text)
        if blocked:
            return ModerationResult(
                blocked=True, verdict="Unsafe", categories=[reason],
                scene=GUARD_SCENE_INPUT, should_refuse_medical_request=True,
            )
        if self.s.guard_shadow:
            self._schedule_shadow(
                text,
                scene=GUARD_SCENE_INPUT,
                timeout_seconds=timeout_seconds or self.s.guard_input_timeout,
                request_id=request_id,
            )
        elif self.s.guard_enforced:
            result = await self.guard.check(
                text, scene=GUARD_SCENE_INPUT,
                timeout_seconds=timeout_seconds or self.s.guard_input_timeout,
                request_id=request_id,
            )
            # V1.1 P1-1：Guard 服务失败/输出无法解析 ≠ 用户违规
            # （blocked=True + verdict=Review + parse_ok=False → agent 走 review/急症固定模板）
            if not result.parse_ok:
                return result
            # 医疗豁免只适用于 Violent 类外伤描述；其他分类不能因出现医疗词而放行。
            if _is_medical_violence_only(result, text):
                result.blocked = False  # 医疗描述放行进急症/风险流程
                result.should_refuse_medical_request = False
            elif result.blocked:
                result.should_refuse_medical_request = True
            return result
        return ModerationResult(verdict="Safe", parse_ok=True, scene=GUARD_SCENE_INPUT)

    async def check_output(
        self, generated: GeneratedConsultation, *, timeout_seconds: float | None = None,
        request_id: str = ""
    ) -> ModerationResult:
        """输出审核（scene=pet_consult_output）：规则一致性 + 模型审核。"""
        rule_result = self.output_rules.check(generated)
        if rule_result.blocked:
            rule_result.scene = GUARD_SCENE_OUTPUT
            return rule_result
        text = self._output_text(generated)
        if self.s.guard_shadow:
            self._schedule_shadow(
                text,
                scene=GUARD_SCENE_OUTPUT,
                timeout_seconds=timeout_seconds or self.s.guard_output_timeout,
                request_id=request_id,
            )
        elif self.s.guard_enforced:
            result = await self.guard.check(
                text, scene=GUARD_SCENE_OUTPUT,
                timeout_seconds=timeout_seconds or self.s.guard_output_timeout,
                request_id=request_id,
            )
            # Guard 故障/解析失败必须 review，不能被医疗关键词豁免。
            if not result.parse_ok:
                return result
            if _is_medical_violence_only(result, text):
                return ModerationResult(
                    verdict="Safe", parse_ok=True, scene=GUARD_SCENE_OUTPUT
                )
            if result.blocked:
                return result
            return ModerationResult(verdict="Safe", parse_ok=True, scene=GUARD_SCENE_OUTPUT)
        return ModerationResult(verdict="Safe", parse_ok=True, scene=GUARD_SCENE_OUTPUT)

    async def close(self) -> None:
        if self._shadow_tasks:
            await asyncio.gather(*tuple(self._shadow_tasks), return_exceptions=True)
        await self.guard.close()

    @staticmethod
    def _output_text(generated: GeneratedConsultation) -> str:
        """覆盖全部用户可见字段，供 enforce/shadow 使用。"""
        return " ".join(
            [generated.summary]
            + generated.possible_explanations
            + generated.what_to_do_now
            + generated.avoid_actions
            + generated.what_to_monitor
            + generated.follow_up_questions
            + (
                [generated.vet_recommendation.reason]
                if generated.vet_recommendation else []
            )
        )

    def _schedule_shadow(
        self,
        text: str,
        *,
        scene: str,
        timeout_seconds: float,
        request_id: str,
    ) -> None:
        task = asyncio.create_task(
            self._record_shadow(
                text,
                scene=scene,
                timeout_seconds=timeout_seconds,
                request_id=request_id,
            )
        )
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)

    async def _record_shadow(
        self,
        text: str,
        *,
        scene: str,
        timeout_seconds: float,
        request_id: str,
    ) -> None:
        result = await self.guard.check(
            text,
            scene=scene,
            timeout_seconds=timeout_seconds,
            request_id=request_id,
        )
        logger.info(
            "guard_shadow_result",
            extra={
                "request_id": request_id,
                "scene": scene,
                "blocked": result.blocked,
                "verdict": result.verdict,
                "parse_ok": result.parse_ok,
                "categories": result.categories,
            },
        )
