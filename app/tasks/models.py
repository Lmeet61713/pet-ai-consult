"""问诊任务模型（Phase 2 §4.4，独立于审核表）。

发布顺序：先写 consult_task + consult_outbox（同事务）→ 发布 RocketMQ →
标记 published。状态机：
registered → queued → scheduled → processing → completed|failed|timeout|cancelled|dead_letter
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ConsultTask(Base):
    """任务主表：所有问诊请求（含急症/简单问答）先登记再流转。"""

    __tablename__ = "consult_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(128), index=True)
    priority: Mapped[str] = mapped_column(String(8), default="P1")  # P0/P1
    fast_path: Mapped[bool] = mapped_column(Boolean, default=False)
    pre_answered: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="registered", index=True)
    # v7.3：分类准入的持久化口径。旧数据由启动迁移按 payload.images 回填。
    task_kind: Mapped[str] = mapped_column(
        String(8), default="text", server_default="text", nullable=False, index=True
    )
    image_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # 输入载荷：text / pet_info / image 哈希与元信息（不存原图）
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    # Worker 完成后的结果（ConsultResponse JSON），API 轮询取回
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ConsultOutbox(Base):
    """待发布表：先落库后发布，发布失败由扫描器补发。"""

    __tablename__ = "consult_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("consult_task.id"), index=True)
    topic: Mapped[str] = mapped_column(String(64))
    tag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConsultTaskEvent(Base):
    """任务事件表：状态流转审计与监控口径。"""

    __tablename__ = "consult_task_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("consult_task.id"), index=True)
    from_status: Mapped[str] = mapped_column(String(24), default="")
    to_status: Mapped[str] = mapped_column(String(24))
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConsultDialogue(Base):
    """效果存档（v1.4 §7.1 PG 版）：每请求一行完整对话，90 天保留。"""

    __tablename__ = "consult_dialogue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str] = mapped_column(String(128), index=True)
    user_text: Mapped[str] = mapped_column(Text, default="")
    pet_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    image_findings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    answer_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    risk_flags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    hit_card_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    rag_decision: Mapped[str | None] = mapped_column(String(24), nullable=True)
    degraded_services: Mapped[list | None] = mapped_column(JSON, nullable=True)
    follow_up_questions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_ms: Mapped[float | None] = mapped_column(nullable=True)
    feedback: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


# 常见查询索引：按状态/时间清理与监控
Index("ix_consult_task_status_created", ConsultTask.status, ConsultTask.created_at)
Index("ix_consult_task_status_updated", ConsultTask.status, ConsultTask.updated_at)
Index(
    "ix_consult_task_kind_status_updated",
    ConsultTask.task_kind,
    ConsultTask.status,
    ConsultTask.updated_at,
)
Index("ix_consult_outbox_unpublished", ConsultOutbox.published, ConsultOutbox.created_at)
