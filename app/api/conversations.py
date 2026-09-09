"""会话接口（v5 §10.3：GET/DELETE，必须校验 tenant+user+conversation 所有权）"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.agent.consult_agent import ConsultAgent
from app.core.dependencies import get_agent
from app.core.exceptions import ConversationNotFoundError
from app.core.security import get_auth_context
from app.schemas.auth import AuthContext
from app.schemas.conversation import ConversationSnapshot

router = APIRouter(prefix="/api/v1", tags=["conversations"])


@router.get("/conversations/{conversation_id}", response_model=ConversationSnapshot)
async def get_conversation(
    conversation_id: str,
    auth: AuthContext = Depends(get_auth_context),
    agent: ConsultAgent = Depends(get_agent),
) -> ConversationSnapshot:
    key = agent.conversation_service.key(auth.tenant_id, auth.user_id, conversation_id)
    snapshot = await agent.conversation_service.load_context(key)
    if not snapshot.turns and not snapshot.meta.turn_count:
        raise ConversationNotFoundError("会话不存在")
    return snapshot


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    auth: AuthContext = Depends(get_auth_context),
    agent: ConsultAgent = Depends(get_agent),
) -> dict:
    key = agent.conversation_service.key(auth.tenant_id, auth.user_id, conversation_id)
    await agent.conversation_service.delete(key)
    return {"deleted": conversation_id}
