"""
统一业务异常体系（v5 §17）

所有异常进入统一 JSON 错误响应：
    {request_id, status, error: {code, message, retryable}}

设计原则：
- 用户侧消息不暴露堆栈、模型地址、Redis 地址和外部 API 内容
- 所有异常继承 PetConsultError 基类，统一处理
- retryable 标记客户端是否可以重试
"""
from __future__ import annotations


class PetConsultError(Exception):
    """业务异常基类，所有自定义异常均继承此类"""

    code = "INTERNAL_ERROR"  # 错误码，用于 API 响应
    http_status = 500  # HTTP 状态码
    retryable = False  # 客户端是否可重试

    def __init__(self, message: str = ""):
        self.message = message or self.__doc__ or self.code
        super().__init__(self.message)


class RequestValidationError(PetConsultError):
    """请求参数校验失败（400）"""
    code = "BAD_REQUEST"
    http_status = 400


class UnauthorizedError(PetConsultError):
    """认证失败（401）：Token 无效/过期/无权限"""
    code = "UNAUTHORIZED"
    http_status = 401


class RateLimitError(PetConsultError):
    """接口限流（429）：请求频率超过阈值"""
    code = "RATE_LIMITED"
    http_status = 429
    retryable = True


class InvalidImageError(PetConsultError):
    """图片无效（400）"""
    code = "INVALID_IMAGE"
    http_status = 400


class ImageTooLargeError(InvalidImageError):
    """图片过大（400）：超过最大字节数限制"""
    code = "IMAGE_TOO_LARGE"
    http_status = 400


class ImageRequestValidationError(InvalidImageError):
    """图片请求/安全校验失败（400）：格式、魔数、像素、解码安全等问题。

    与"内容质量不可用"（Vision 判定 unusable → provisional）和
    "Vision 服务失败"（超时/不可用 → 文本降级）严格区分。
    """
    code = "INVALID_IMAGE_REQUEST"
    http_status = 400


class UnsafeInputError(PetConsultError):
    """不安全的输入内容（200）：业务态 refuse，由 agent 转换为拒绝响应"""
    code = "UNSAFE_INPUT"
    http_status = 200  # 业务态 refuse，由 agent 转换


class ExternalServiceError(PetConsultError):
    """外部服务调用失败（503）：可重试"""
    code = "EXTERNAL_SERVICE_ERROR"
    http_status = 503
    retryable = True


class ExternalServiceTimeout(ExternalServiceError):
    """外部服务调用超时（503）"""
    code = "EXTERNAL_SERVICE_TIMEOUT"
    http_status = 503
    retryable = True


class ModelOutputValidationError(PetConsultError):
    """模型输出校验失败（502）：JSON 解析失败、字段校验不通过"""
    code = "MODEL_OUTPUT_INVALID"
    http_status = 502
    retryable = True


class ConversationConflictError(PetConsultError):
    """会话冲突（409）：同一会话被并发操作"""
    code = "CONVERSATION_CONFLICT"
    http_status = 409
    retryable = True


class QueueBusyError(PetConsultError):
    """队列活动任务达到准入上限，调用方应按 Retry-After 稍后重试。"""
    code = "QUEUE_BUSY"
    http_status = 503
    retryable = True


class IdempotencyConflictError(PetConsultError):
    """同一 Idempotency-Key 被用于不同请求内容（409）。"""
    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 409
    retryable = False


class RedisUnavailable(PetConsultError):
    """Redis 不可用：降级处理，不阻断主链路"""
    code = "REDIS_UNAVAILABLE"
    http_status = 503
    retryable = True


class VisionUnavailable(PetConsultError):
    """视觉服务不可用（503）：降级为纯文本问诊"""
    code = "VISION_UNAVAILABLE"
    http_status = 503
    retryable = True


class VisionTimeout(PetConsultError):
    """视觉服务超时（503）"""
    code = "VISION_TIMEOUT"
    http_status = 503
    retryable = True


class VisionOutputInvalid(PetConsultError):
    """视觉输出无效（502）：模型输出格式错误"""
    code = "VISION_OUTPUT_INVALID"
    http_status = 502
    retryable = True


class KnowledgeConsultUnavailable(PetConsultError):
    """知识问诊服务不可用（503）"""
    code = "KNOWLEDGE_CONSULT_UNAVAILABLE"
    http_status = 503
    retryable = True


class RequestDeadlineExceeded(PetConsultError):
    """请求处理超时（504）：超过总 deadline 限制"""
    code = "DEADLINE_EXCEEDED"
    http_status = 504
    retryable = False


class ConversationNotFoundError(PetConsultError):
    """会话不存在（404）"""
    code = "CONVERSATION_NOT_FOUND"
    http_status = 404
    retryable = False