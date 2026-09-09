"""会话服务：读取上下文 / 保存轮次 / 摘要 / 锁（v5 §15 / V1.1 P0-2）"""
from __future__ import annotations

import logging

import redis.exceptions as redis_exc

from app.core.config import Settings
from app.core.constants import VetUrgency
from app.core.exceptions import RedisUnavailable
from app.repositories.conversation_repository import ConversationRepository
from app.schemas.conversation import (
    ConversationKey,
    ConversationSnapshot,
    ConversationTurn,
)
from app.utils.time import utc_now_iso

logger = logging.getLogger(__name__)


class ConversationService:
    def __init__(self, settings: Settings, repo: ConversationRepository):
        self.s = settings
        self.repo = repo

    def key(self, tenant_id: str, user_id: str, conversation_id: str) -> ConversationKey:
        return ConversationKey(
            namespace=self.s.redis_namespace,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
        )

    async def load_context(self, key: ConversationKey) -> ConversationSnapshot:
        """读历史 + 摘要。

        V1.1 P0-2：Redis 不可用向上抛 RedisUnavailable（agent 记录 degraded 后
        无记忆单轮继续）；不再吞掉异常导致降级标记丢失。
        """
        try:
            return await self.repo.load(key)
        except RedisUnavailable:
            raise
        except redis_exc.RedisError as exc:
            raise RedisUnavailable("会话历史不可用，已降级为无记忆单轮") from exc

    async def save_turn(self, key: ConversationKey, turn: ConversationTurn) -> None:
        try:
            await self.repo.append(key, turn)
        except Exception as exc:  # noqa: BLE001 - 历史写失败不影响本轮返回
            logger.warning("会话保存失败（本轮仍返回）: %s", exc)

    async def save_summary(self, key: ConversationKey, summary: str) -> None:
        try:
            await self.repo.save_summary(key, summary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话摘要保存失败: %s", exc)

    async def delete(self, key: ConversationKey) -> None:
        await self.repo.delete(key)

    async def acquire_lock(
        self, key: ConversationKey, *, owner_token: str, lease_seconds: float | None = None
    ) -> bool:
        return await self.repo.acquire_lock(key, owner_token=owner_token, lease_seconds=lease_seconds)

    async def wait_for_lock(
        self, key: ConversationKey, *, owner_token: str, timeout_s: float | None = None
    ) -> bool:
        return await self.repo.wait_for_lock(key, owner_token=owner_token, timeout_s=timeout_s)

    async def release_lock(self, key: ConversationKey, *, owner_token: str) -> None:
        await self.repo.release_lock(key, owner_token=owner_token)

    @staticmethod
    def build_turn(
        *,
        user_text: str,
        pet_info: dict,
        image_findings: list,
        risk_level,
        risk_flags: list[str],
        assistant_status: str,
        answer_mode=None,
        vet_urgency=None,
        assistant_answer: str,
        model_versions: dict,
        follow_up_questions: list[str] | None = None,
        case_facts: dict | None = None,
    ) -> ConversationTurn:
        return ConversationTurn(
            turn_id="",
            created_at=utc_now_iso(),
            user_text=user_text[:8000],
            pet_info=pet_info,
            image_findings=image_findings,
            risk_level=risk_level,
            risk_flags=risk_flags,
            assistant_status=assistant_status,
            answer_mode=answer_mode,
            vet_urgency=vet_urgency or VetUrgency.NONE,
            assistant_answer=assistant_answer,
            follow_up_questions=follow_up_questions or [],
            case_facts=case_facts or {},
            model_versions=model_versions,
        )

    async def close(self) -> None:  # pragma: no cover - repo 无独立连接
        pass
