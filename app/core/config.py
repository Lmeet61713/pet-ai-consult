"""
应用配置管理

基于 pydantic-settings 从环境变量 /.env 文件读取配置。
所有运行时配置集中管理，支持多环境（development/test/production）差异化配置。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # 项目根目录
CONFIG_DIR = PROJECT_ROOT / "configs"  # 配置文件目录


class Settings(BaseSettings):
    """应用全局配置（通过 pydantic-settings 从环境变量或 .env 文件加载）

    配置分组：
    - 基础应用配置（环境、主机、日志级别）
    - JWT 鉴权配置
    - Mock 开关（本地开发用）
    - 外部服务配置（Vision、KnowledgeConsult、Guard）
    - RAG 检索配置
    - Redis 配置
    - 队列化配置（Phase 2）
    - 限流配置
    """
    model_config = SettingsConfigDict(env_file=[".env", ".env.secrets"], extra="ignore")

    # ──────────────────────────────────────────────
    # 基础应用配置
    # ──────────────────────────────────────────────
    app_env: Literal["development", "test", "production"]  # 运行环境
    app_host: str = "127.0.0.1"  # 服务监听地址
    app_log_level: str = "INFO"  # 日志级别
    app_secret_key: str = "change-me"  # 应用密钥

    # ──────────────────────────────────────────────
    # JWT 鉴权
    # ──────────────────────────────────────────────
    jwt_algorithm: str = "HS256"  # JWT 签名算法
    jwt_signing_secret: str = ""  # JWT 签名密钥
    jwt_issuer: str = "business-auth"  # JWT 签发者
    jwt_audience: str = "pet-consult"  # JWT 受众
    jwt_required_scope: str = "pet:consult"  # 问诊所需权限范围

    # ──────────────────────────────────────────────
    # 日志脱敏
    # ──────────────────────────────────────────────
    log_hash_secret: str = ""  # 日志脱敏 HMAC 密钥

    # ──────────────────────────────────────────────
    # 本地开发 Mock 开关
    # ──────────────────────────────────────────────
    mock_mode: bool = True  # 全局 Mock 模式（生产环境禁止）
    mock_vision: bool = True  # Mock 视觉服务
    mock_knowledge_consult: bool = True  # Mock 知识问诊服务
    mock_guard: bool = True  # Mock 审核服务

    # ──────────────────────────────────────────────
    # Vision 视觉服务配置
    # ──────────────────────────────────────────────
    vision_gateway_base_url: str = "http://127.0.0.1:8102"  # VisionGateway 地址
    vision_scene: str = "pet_consult_image"  # 视觉场景标识
    vision_priority: int = 0  # 视觉请求优先级
    vision_timeout_seconds: float = 15.0  # 视觉超时时间
    # 视觉结构化输出长度。代码安全默认保持 512；图片回归通过后可在环境中设为 320/384。
    vision_max_tokens: int = Field(default=512, ge=128, le=1024)
    vision_cache_enabled: bool = True  # 是否启用视觉缓存
    vision_cache_ttl_seconds: int = Field(default=86400, ge=60, le=604800)  # 视觉缓存 TTL
    consult_vision_model_name: str = "Qwen3.5-4B"  # 视觉模型名称

    # ──────────────────────────────────────────────
    # KnowledgeConsult 知识问诊 API 配置
    # ──────────────────────────────────────────────
    knowledge_provider: Literal["deepseek_official", "local_openai"] = "deepseek_official"  # 知识问诊供应商
    knowledge_api_base_url: str = ""  # API 地址
    knowledge_api_key: str = ""  # API 密钥
    knowledge_model: str = ""  # 模型名称
    knowledge_thinking_mode: Literal["disabled", "enabled"] = "disabled"  # 思考模式
    knowledge_timeout_seconds: float = 20.0  # API 超时
    knowledge_connect_timeout_seconds: float = Field(default=10.0, gt=0)  # 连接超时
    knowledge_connect_retries: int = Field(default=1, ge=0, le=3)  # 连接重试次数
    # 本地思考型模型（Qwen3.5 系列）默认输出 Thinking Process；置 True 时请求体
    # 携带 chat_template_kwargs={"enable_thinking": false} 关闭思考输出。
    knowledge_local_disable_thinking: bool = True
    # 本地模型采样温度（vLLM 默认 1.0 随机性过大，输出不稳定）
    knowledge_local_temperature: float = Field(default=0.6, ge=0, le=2)
    # 生成长度上限（0 = 不设置，用服务端默认）
    knowledge_max_tokens: int = Field(default=0, ge=0)
    deepseek_contract_file: str = ""  # DeepSeek 契约验证文件路径

    # ──────────────────────────────────────────────
    # RAG 检索增强生成配置
    # ──────────────────────────────────────────────
    rag_mode: Literal["off", "shadow", "grounded"] = "shadow"  # RAG 模式
    rag_index_path: str = ""  # 知识库索引路径
    rag_top_k: int = Field(default=4, ge=1, le=20)  # 检索返回 TOP K 结果
    rag_score_threshold: float = Field(default=0.24, ge=0, le=1)  # 检索分数阈值
    rag_emergency_shadow: bool = True  # 急症规则 Shadow 模式
    # 简单问答直答通道（fast_path）：命中 simple_owner_question 卡片且置信超阈值时
    # 不调生成模型，直接渲染卡片内容（v1.2 §4.3）
    rag_fast_answer: bool = False
    rag_fast_answer_threshold: float = Field(default=0.40, ge=0, le=1)  # 快速回答阈值
    # 追问查缺：命中卡片后对照 questions_to_ask 检查用户是否已提供关键信息
    rag_followup_check: bool = True
    # 混合检索（词面 + BGE-M3 向量; 模型路径为空则回退纯词面）
    rag_hybrid_alpha: float = Field(default=0.7, ge=0, le=1)  # 混合检索词面权重
    rag_hybrid_threshold: float = Field(default=0.32, ge=0, le=1)  # 混合检索阈值
    rag_embedding_model_path: str = ""  # 向量模型路径

    # ──────────────────────────────────────────────
    # Guard 审核服务配置
    # ──────────────────────────────────────────────
    guard_base_url: str = "http://127.0.0.1:8103"  # Guard 服务地址
    guard_input_timeout_ms: int = 1500  # 输入审核超时（毫秒）
    guard_output_timeout_ms: int = 1500  # 输出审核超时（毫秒）
    guard_mode: Literal["off", "shadow", "enforce"] = "off"  # 审核模式
    consult_guard_model_name: str = "Qwen3Guard-Gen-0.6B"  # 审核模型名称

    # ──────────────────────────────────────────────
    # Redis 配置
    # ──────────────────────────────────────────────
    redis_url: str = "redis://127.0.0.1:6379/0"  # Redis 连接地址
    redis_password: str = ""  # Redis 密码
    redis_key_prefix: str = Field(default="pet_consult", pattern=r"^[a-z0-9_-]+$")  # Redis 键前缀
    redis_ttl_seconds: int = 7 * 86400  # Redis 键默认 TTL（7 天）
    max_conversation_turns: int = 20  # 最大会话轮次

    # ──────────────────────────────────────────────
    # 队列化配置（Phase 2 §4.4：任务表 + Outbox + RocketMQ）
    # ──────────────────────────────────────────────
    # 空 = 队列化关闭（Phase 1 直连模式，当前默认）
    consult_database_url: str = ""
    # 2026-08-21: PG 密码独立传入（URL 拼密码遇 @ 等特殊字符会解析错）
    consult_database_password: str = ""
    consult_mq_enabled: bool = False
    # 问诊独立 RocketMQ 实例（集群 pet-consult，namesrv :9877 / broker :10912），
    # 与审核系统 pet-moderation 的 :9876/:10911 完全隔离，互不影响。
    consult_mq_namesrv_addr: str = "127.0.0.1:9877"
    # pyrocketmq 发布端依赖的 RocketMQ Java 客户端 classpath（4.9.8 发行包 lib/*）
    consult_mq_classpath: str = "/root/autodl-tmp/tools/rocketmq-all-4.9.8-bin-release/lib/*"
    consult_mq_topic: str = "consult-tasks"
    consult_mq_group: str = "consult-worker-group"
    consult_mq_dlq_topic: str = "consult-tasks-dlq"
    consult_mq_max_retry: int = Field(default=3, ge=0, le=10)
    consult_outbox_scan_seconds: float = Field(default=5.0, gt=0)
    # 问诊 Worker 并发数（v1.5：多 Worker 共享 GPU，FOR UPDATE SKIP LOCKED 抢占）
    # 2026-08-25: 上限按 v7.3 压测候选放宽到 32（候选 20/24/32；33 拒绝）
    consult_worker_count: int = Field(default=10, ge=1, le=32)
    # API 等待任务结果超时（排队削峰的有界等待）：突发时排队任务在此时间内完成即返回，
    # 超时返回 TASK_TIMEOUT(retryable) 供客户端重试；nginx 网关超时须大于此值（部署对齐 75s）。
    # 2026-08-20：180s→60s，避免"排队过久才放弃"；压测后按容量微调。
    consult_wait_result_timeout_seconds: float = Field(default=60.0, gt=1)
    # 直答同步车道并发上限（Redis 原子计数；超限溢出到队列车道）
    fast_sync_max_concurrent: int = Field(default=10, ge=1, le=100)
    # 活动任务原子准入上限（显式设 0 可关闭）：
    # registered/queued/scheduled/processing 总数达到阈值时
    # 新请求直接返回 503 QUEUE_BUSY(retryable)，防止极端积压拖垮所有请求。
    # 日常靠"排队+有界等待"消化突发；压测后再按可接受等待时间调整。
    queue_max_pending: int = Field(default=12, ge=0)
    # v7.3 分类准入。TEXT_MAX_ACTIVE 与 IMAGE_MAX_ACTIVE 必须同时配置；两者均为 0
    # 时继续使用 QUEUE_MAX_PENDING 的 v7.2 全局口径，便于滚动升级和一键回滚。
    text_max_active: int = Field(default=0, ge=0)
    image_max_active: int = Field(default=0, ge=0)
    # 图片请求按图片数消耗视觉槽位；0 表示只限制图片问诊请求数。
    image_max_active_slots: int = Field(default=0, ge=0)
    # registered/queued/processing 超过该时长会被回收为 timeout，避免异常任务永久占满准入槽位。
    queue_active_stale_seconds: int = Field(default=120, ge=30, le=3600)
    # SSE 队列流总等待上限：入队后等待结果超过此时长则发 error(QUEUE_TIMEOUT, retryable) 收尾，
    # 避免客户端无限挂起；须小于 nginx 的 SSE 代理超时。
    consult_sse_max_wait_seconds: float = Field(default=60.0, gt=1)
    consult_sse_stream_ttl_seconds: int = Field(default=300, ge=60, le=3600)

    # PostgreSQL 队列连接池。10 个 Worker + API/监控查询需要显式预留连接。
    consult_db_pool_size: int = Field(default=20, ge=5, le=100)
    consult_db_max_overflow: int = Field(default=10, ge=0, le=100)
    consult_db_pool_timeout_seconds: float = Field(default=10.0, gt=0)

    # 同会话串行锁。实际租约至少为"总请求超时 + 15s"。
    # 用于防止同一会话的多个请求并发处理导致数据竞争。
    conversation_lock_wait_seconds: float = Field(default=20.0, gt=0, le=60)  # 等待获取锁的超时时间
    conversation_lock_lease_seconds: float = Field(default=95.0, gt=0, le=300)  # 锁租约时长（需覆盖整个处理窗口）

    # ──────────────────────────────────────────────
    # 效果观测：对话存档（v1.2 §7，Phase 1 JSONL 轻量版）
    # ──────────────────────────────────────────────
    # 每请求一行 JSON 追加到配置路径：输入、中间决策、输出、分段耗时、降级标记。
    # 空路径 = 关闭（测试默认关）。Phase 2 队列化后迁移到 PostgreSQL consult_dialogue 表。
    dialogue_archive_path: str = ""

    # ──────────────────────────────────────────────
    # 图片限制
    # ──────────────────────────────────────────────
    max_image_bytes: int = 5 * 1024 * 1024  # 单张图片最大字节数（5MB）
    max_image_edge: int = 1024  # 图片长边最大像素（超长边按比例压缩）
    max_image_pixels: int = 20_000_000  # 图片最大像素数（防解压炸弹攻击）

    # ──────────────────────────────────────────────
    # 超时配置
    # ──────────────────────────────────────────────
    consult_total_timeout_seconds: float = 45.0  # 问诊总超时（整个请求的处理时限）
    # 安全重写阶段预算（本地模型生成较慢时调大，如 20s）
    safety_rewrite_timeout_seconds: float = 15.0  # 安全重写超时（受 deadline 约束）

    # ──────────────────────────────────────────────
    # 功能开关
    # ──────────────────────────────────────────────
    enable_guard: bool = True  # 兼容旧环境变量，运行时行为只由 GUARD_MODE 决定
    enable_admin_api: bool = False  # 是否启用管理 API（生产环境禁止）

    # ──────────────────────────────────────────────
    # 鉴权/限流开关（2026-08-20：内部部署可跳过 JWT 鉴权与限流）
    # ──────────────────────────────────────────────
    # auth_skip=True 时跳过 JWT 校验，改用 X-User-Id 请求头做用户隔离（与 mock 模式行为一致，
    # 但保持真实 Redis/模型/队列，不会引入 fakeredis）；默认 False=JWT 校验。
    auth_skip: bool = False
    # rate_limit_enabled=False 时限流直接放行；默认 True=按 RATE_LIMIT_MAX 限流。
    rate_limit_enabled: bool = True

    # ──────────────────────────────────────────────
    # 限流配置
    # ──────────────────────────────────────────────
    rate_limit_max: int = 30  # 限流窗口内最大请求数
    rate_limit_window_seconds: int = 60  # 限流窗口大小（秒）

    # ------------------------------------------------------------ 属性

    @property
    def is_prod(self) -> bool:
        return self.app_env == "production"

    @property
    def guard_input_timeout(self) -> float:
        return self.guard_input_timeout_ms / 1000.0

    @property
    def guard_output_timeout(self) -> float:
        return self.guard_output_timeout_ms / 1000.0

    @property
    def guard_enforced(self) -> bool:
        return self.guard_mode == "enforce"

    @property
    def guard_shadow(self) -> bool:
        return self.guard_mode == "shadow"

    @property
    def rag_shadow(self) -> bool:
        return self.rag_mode in {"shadow", "grounded"}

    @property
    def rag_grounded(self) -> bool:
        return self.rag_mode == "grounded"

    @property
    def knowledge_timeout(self) -> float:
        return self.knowledge_timeout_seconds

    @property
    def knowledge_connect_timeout(self) -> float:
        return self.knowledge_connect_timeout_seconds

    @property
    def redis_namespace(self) -> str:
        """Redis 逻辑隔离前缀；同实例/同 DB 下不同环境也不会碰撞。"""
        return f"{self.redis_key_prefix}:{self.app_env}"

    @property
    def classified_admission_enabled(self) -> bool:
        return self.text_max_active > 0 and self.image_max_active > 0

    @model_validator(mode="after")
    def validate_classified_admission(self) -> "Settings":
        configured = (self.text_max_active > 0, self.image_max_active > 0)
        if configured[0] != configured[1]:
            raise ValueError(
                "TEXT_MAX_ACTIVE and IMAGE_MAX_ACTIVE must be configured together"
            )
        if self.image_max_active_slots > 0 and not self.classified_admission_enabled:
            raise ValueError(
                "IMAGE_MAX_ACTIVE_SLOTS requires classified admission limits"
            )
        if (
            self.image_max_active_slots > 0
            and self.image_max_active_slots < self.image_max_active
        ):
            raise ValueError(
                "IMAGE_MAX_ACTIVE_SLOTS cannot be smaller than IMAGE_MAX_ACTIVE"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()