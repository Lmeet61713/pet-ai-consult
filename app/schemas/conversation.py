"""会话历史结构（v5 §15.2）"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.core.constants import AnswerMode, RiskLevel, VetUrgency
from app.schemas.image import VisionFinding


class ConversationKey(BaseModel):
    # 只用于 Redis 内部寻址，不进入对外 ConversationSnapshot 响应。
    namespace: str = Field(default="pet_consult:test", exclude=True)
    tenant_id: str
    user_id: str
    conversation_id: str

    @property
    def meta_key(self) -> str:
        """会话元数据键"""
        return (
            f"{self.namespace}:consult:{self.tenant_id}:{self.user_id}:"
            f"{self.conversation_id}:meta"
        )

    @property
    def turns_key(self) -> str:
        """会话轮次键"""
        return (
            f"{self.namespace}:consult:{self.tenant_id}:{self.user_id}:"
            f"{self.conversation_id}:turns"
        )

    @property
    def summary_key(self) -> str:
        """会话摘要键"""
        return (
            f"{self.namespace}:consult:{self.tenant_id}:{self.user_id}:"
            f"{self.conversation_id}:summary"
        )

    @property
    def lock_key(self) -> str:
        """会话锁键"""
        return (
            f"{self.namespace}:lock:consult:{self.tenant_id}:{self.user_id}:"
            f"{self.conversation_id}"
        )


class ConversationTurn(BaseModel):
    """一轮完整记录（v5 §15.2 turns List 每项）"""

    turn_id: str                                                    # 轮次 ID
    created_at: str                                                # 创建时间
    user_text: str = ""                                            # 用户输入文本
    pet_info: dict = Field(default_factory=dict)                    # 宠物信息
    image_findings: list[VisionFinding] = Field(default_factory=list)  # 图像识别结果
    risk_level: RiskLevel = RiskLevel.LOW                          # 风险等级
    risk_flags: list[str] = Field(default_factory=list)            # 风险标志
    assistant_status: str = ""                                     # 机器人状态
    answer_mode: AnswerMode | None = None                          # 回答模式
    vet_urgency: VetUrgency = VetUrgency.NONE                      # 问诊 urgency
    assistant_answer: str = ""                                     # 机器人回复文本
    follow_up_questions: list[str] = Field(default_factory=list)   # 进一步问题
    case_facts: dict = Field(default_factory=dict)                # 案例事实
    model_versions: dict = Field(default_factory=dict)            # 模型版本


class ConversationMeta(BaseModel):
    """会话元数据（v5 §15.2 meta 字段）"""
    created_at: str = ""       # 创建时间
    updated_at: str = ""       # 更新时间
    turn_count: int = 0       # 轮次数量
    schema_version: str = "2"       # 数据库版本
    model_version: str = ""       # 模型版本


class ConversationSnapshot(BaseModel):
    """repository.load() 返回：历史轮次 + 摘要"""

    key: ConversationKey       # 会话键值
    meta: ConversationMeta = Field(default_factory=ConversationMeta)       # 会话元数据
    turns: list[ConversationTurn] = Field(default_factory=list)       # 会话轮次
    summary: str = ""       # 会话摘要
