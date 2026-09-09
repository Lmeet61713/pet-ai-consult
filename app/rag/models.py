"""RAG 模块的内部结果契约（数据结构定义）。

本模块定义检索器（ShadowRetriever / HybridRetriever）与上层 Agent 之间
传递的数据结构：
- RagDecisionStatus：检索决策状态枚举（本次检索能否支撑回答）
- RagHit：单张命中知识卡片的评分与引用信息
- RagResult：一次检索的完整结果（决策 + 命中列表 + 版本/耗时元数据）

这些结构是 RAG 子系统的对外契约，Agent 只依赖本模块，不直接接触
知识卡片原始 JSON（卡片资产的加载/校验见 loader.py）。
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class RagDecisionStatus(StrEnum):
    """RAG 检索决策状态：表示本次检索的结论，决定上层如何使用检索结果。

    取值含义：
    - SUFFICIENT：命中高相关卡片，证据充分，可用于 grounded 回答/卡片直答
    - INSUFFICIENT：未命中或相关性不足（含含糊查询、歧义查询、低分命中），
      不向生成上下文注入卡片内容
    - POLICY_RESTRICTED：命中政策限制词（如剂量/处方/换药），禁止用卡片
      回答用药问题，转由安全链路处理
    - OUT_OF_SCOPE：超出知识库范围（预留）
    - UNAVAILABLE：知识资产未配置/校验失败/文件缺失，检索不可用
    - DISABLED：配置关闭 RAG（预留）
    """

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    POLICY_RESTRICTED = "policy_restricted"
    OUT_OF_SCOPE = "out_of_scope"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class RagHit(BaseModel):
    """单张命中知识卡片的检索结果（RagResult.hits 中的元素）。

    一张卡片对应知识库中的一个知识单元（如"猫呕吐分诊""幼犬驱虫日程"），
    其下包含若干事实（facts）与来源引用（evidence_refs）。
    """

    card_id: str            # 卡片 ID（如 "V17-CAT-PED-001"）
    fact_ids: list[str] = Field(default_factory=list)   # 卡片下事实 ID 列表
    source_ids: list[str] = Field(default_factory=list)   # 引用的外部来源 ID 列表（可溯源）
    semantic_score: float = 0.0   # 语义（向量）相似度分；纯词法模式恒为 0.0
    keyword_score: float = 0.0   # 词法关键词重叠分（0~1）
    final_score: float = 0.0   # 最终融合分（词法/向量加权后，0~1）


class RagResult(BaseModel):
    """一次 RAG 检索的完整结果（检索器 search() 的返回值）。

    上层（ConsultAgent）依据 decision 决定：
    - SUFFICIENT + 高分简单问答卡 → 卡片直答（fast answer，不调生成模型）
    - SUFFICIENT → 取卡片 questions_to_ask 参与完整度追问、
      build_grounded_evidence() 投影为模型上下文
    - 其余状态 → 不注入卡片，按普通问诊流程处理
    """

    query: str            # 原始查询文本（用户本轮文字）
    decision: RagDecisionStatus   # 检索决策状态（见枚举说明）
    reason_codes: list[str] = Field(default_factory=list)   # 机器可读原因码（如 vague_general_query/low_relevance）
    hits: list[RagHit] = Field(default_factory=list)   # 命中卡片列表（按 final_score 降序，最多 top_k 张）
    top_score: float | None = None   # 最高分卡片的 final_score；无命中为 None
    index_version: str            # 知识库索引版本号（来自资产 validation_report）
    embedding_model_version: str = "none-lexical-shadow"  # 嵌入模型版本；纯词法 Shadow 模式为默认占位值
    retrieval_ms: float = 0.0   # 本次检索耗时（毫秒），用于监控
