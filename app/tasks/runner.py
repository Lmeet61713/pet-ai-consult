"""Worker 与问诊主流程桥接（Phase 2：task → ConsultCommand → agent）。

【文件定位】
这是队列化执行路径的"桥梁"文件，负责在 Worker 消费任务时，
将数据库中的 task.payload 重建为 ConsultCommand 对象，供 Agent 执行。

【核心函数】
1. build_command_from_task(task) → ConsultCommand
   从 ConsultTask 的 payload 重建 ConsultCommand 对象
   - 读取临时图片文件路径（不存原图到数据库）
   - 解析宠物信息（兼容单对象和数组）
   - 构建 AuthContext（tenant_id / user_id）

2. cleanup_task_temp_files(task) → None
   Worker 处理完成后清理该任务的图片临时目录（幂等）
   - 目录路径：/tmp/pet-consult-images/{request_id}/
   - 使用 shutil.rmtree(ignore_errors=True) 确保幂等

【数据流】
HTTP 请求 → _register_queue_task() → 图片写临时文件 + payload 存 PG
  → RocketMQ 发布 → Worker 消费 → build_command_from_task(task)
  → ConsultCommand → agent.run(command) → 结果存 PG → cleanup_task_temp_files(task)

【设计要点】
- 图片不存数据库：通过临时文件路径传递，避免大对象存储
- 容错处理：临时文件不存在时记录警告但不阻断任务
- 宠物信息容错：单条坏数据不阻断整个任务
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.schemas.auth import AuthContext
from app.schemas.consult import ConsultCommand
from app.schemas.image import ProcessedImage
import shutil
import tempfile

from app.schemas.pet import PetInfo
from app.tasks.models import ConsultTask

logger = logging.getLogger(__name__)


def build_command_from_task(task: ConsultTask) -> ConsultCommand:
    """从任务 payload 重建 ConsultCommand（图片经临时文件传递，不存原图）。

    【职责】
    将数据库中存储的 ConsultTask 转换为 Agent 可执行的 ConsultCommand 对象。
    这是 Worker 消费任务后的第一步，负责：
    1. 从 payload["images"] 读取临时文件路径，构建 ProcessedImage 列表
    2. 从 payload["pet_info"] 解析宠物信息
    3. 从 payload["pets"] 解析完整宠物列表（多宠支持）
    4. 从 task 字段构建 AuthContext（tenant_id / user_id）

    【图片处理】
    - 临时文件路径：/tmp/pet-consult-images/{request_id}/{image_id}.{format}
    - 文件不存在时记录警告（Worker 可能已清理），但不阻断任务
    - ProcessedImage 不存储二进制数据，只存路径（Agent 内按需读取）

    【宠物信息解析】
    - pet_info：当前问诊指向的宠物（单对象）
    - pets：完整宠物列表（数组，多宠支持）
    - pet_ref：本次问诊指定的宠物标识（name 或下标）
    - 解析失败时记录警告，单条坏数据不阻断整个任务

    :param task: ConsultTask 对象（从数据库查询得到）
    :return: ConsultCommand 对象（Agent 的输入契约）
    """
    payload = task.payload or {}

    # 重建图片列表（从临时文件路径）
    images: list[ProcessedImage] = []
    for meta in payload.get("images", []):
        temp_path = meta.get("temp_path", "")
        if temp_path and Path(temp_path).is_file():
            images.append(
                ProcessedImage(
                    image_id=meta.get("image_id", ""),
                    filename=meta.get("filename", "upload"),
                    format=meta.get("format", "JPEG"),
                    temp_path=temp_path,
                    width=meta.get("width", 0),
                    height=meta.get("height", 0),
                    sha256=meta.get("sha256", ""),
                )
            )
        else:
            # 临时文件不存在（可能已被清理或 Worker 重启）
            logger.warning("worker_image_temp_missing", extra={"task_id": task.id})

    # 解析宠物信息（当前问诊指向的宠物）
    pet_info = None
    if payload.get("pet_info"):
        pet_info = PetInfo.model_validate(payload["pet_info"])

    # 解析完整宠物列表（多宠支持）
    pets = []
    for item in payload.get("pets") or []:
        try:
            pets.append(PetInfo.model_validate(item))
        except Exception:  # noqa: BLE001 - 单条坏数据不阻断整个任务
            logger.warning("worker_pet_invalid", extra={"task_id": task.id})

    # 构建 ConsultCommand
    return ConsultCommand(
        request_id=task.request_id,
        auth=AuthContext(tenant_id=task.tenant_id, user_id=task.user_id),
        conversation_id=task.conversation_id,
        text=payload.get("text", ""),
        images=images,
        pets=pets,
        pet_ref=payload.get("pet_ref"),
        pet_info=pet_info,
    )


def cleanup_task_temp_files(task: ConsultTask) -> None:
    """Worker 处理完成后清理该任务的图片临时目录（幂等）。

    【职责】
    删除任务执行过程中创建的临时图片文件目录，避免磁盘空间泄漏。
    目录路径：/tmp/pet-consult-images/{request_id}/

    【幂等设计】
    - 使用 shutil.rmtree(ignore_errors=True)
    - 目录不存在时不抛异常（可能已被其他进程清理）
    - 可安全多次调用

    【调用时机】
    - normal_handler 的 finally 块（无论成功失败都清理）
    - Worker 任务完成后自动执行

    :param task: ConsultTask 对象（用于获取 request_id）
    """
    base = Path(tempfile.gettempdir()) / "pet-consult-images" / task.request_id
    shutil.rmtree(base, ignore_errors=True)