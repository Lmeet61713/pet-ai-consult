"""RocketMQ 发布客户端（Phase 2 §4.4，topic=consult-tasks）。

实现说明（2026-08-18 变更）：
- 发布端使用 pyrocketmq（JPype 桥接 Java rocketmq-client 4.9.8），
  而不是 rocketmq-client-python（C++ 封装）。
- 原因：rocketmq-client-cpp 2.2.0 与 RocketMQ 5.3.2 namesrv 的 topic 路由
  协议不兼容（Producer 报 "No route info of this topic"），Java 5.3.2/4.9.8
  客户端与 pyrocketmq 均实测可用。
- 依赖：java（>=8）+ pyrocketmq + jpype1；classpath 指向 RocketMQ 4.9.8
  发行包的 lib/*（config.consult_mq_classpath）。
- 发布走 asyncio.to_thread，不阻塞事件循环；JVM 与 Producer 进程内单例懒加载。
- 未安装依赖时队列化应关闭（consult_mq_enabled=false），发布失败抛
  MessagePublishError 由 OutboxScanner 重试。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger(__name__)


class MessagePublishError(Exception):
    """RocketMQ 发布失败（调用方决定重试/降级）。"""


class ConsultMessage(BaseModel):
    """队列消息载荷：只放 task_id 与关键路由信息，Worker 从 PG 取完整任务。"""

    model_config = ConfigDict(extra="forbid")

    task_id: int
    request_id: str
    priority: str
    fast_path: bool
    pre_answered: bool
    task_kind: Literal["text", "image"] = "text"
    image_count: int = 0


class ConsultMqPublisherProto(Protocol):
    async def publish(self, message: ConsultMessage) -> None: ...

    async def close(self) -> None: ...


class RocketMQConsultPublisher:
    """pyrocketmq 发布端：懒加载 JVM + Producer，同一进程复用连接。"""

    def __init__(self, *, namesrv_addr: str, topic: str, classpath: str) -> None:
        self._namesrv_addr = namesrv_addr
        self._topic = topic
        self._classpath = classpath
        self._producer: Any | None = None
        self._lock = asyncio.Lock()

    async def publish(self, message: ConsultMessage) -> None:
        try:
            async with self._lock:
                await asyncio.to_thread(self._publish_sync, message)
        except Exception as exc:  # noqa: BLE001 - 统一映射
            raise MessagePublishError(f"RocketMQ publish failed: {exc}") from exc

    async def close(self) -> None:
        if self._producer is not None:
            producer, self._producer = self._producer, None
            await asyncio.to_thread(producer.shutdown)

    def _publish_sync(self, message: ConsultMessage) -> None:
        import jpype
        import jpype.imports  # noqa: F401 - 使 java.* 可导入

        if not jpype.isJVMStarted():
            jpype.startJVM("-Xmx512m", classpath=[self._classpath])
        from pyrocketmq.client.producer import Producer
        from pyrocketmq.common.message import Message

        if self._producer is None:
            producer = Producer()
            producer.setNamesrvAddr(self._namesrv_addr)
            producer.setProducerGroup("pet-consult-producer")
            producer.start()
            self._producer = producer
        mq_message = Message(topic=self._topic, body=message.model_dump_json().encode())
        mq_message.setKeys([message.request_id])
        mq_message.setTags(message.priority)  # P0/P1 tag，Scheduler 按优先级消费
        self._producer.send(mq_message)


def parse_consult_message(body: bytes | str) -> ConsultMessage:
    try:
        return ConsultMessage.model_validate_json(body)
    except ValidationError as exc:
        raise ValueError("invalid consult-tasks message schema") from exc
