"""RAG 模块的内部结果契约（数据结构定义）。

本模块定义检索器（ShadowRetriever / HybridRetriever）与上层 Agent 之间
传递的数据结构：
- RagDecisionStatus：检索决策状态枚举（本次检索能否支撑回答）
- RagHit：单张命中知识卡片的评分与引用信息
- RagResult：一次检索的完整结果（决策 + 命中列表 + 版本/耗时元数据）

这些结构是 RAG 子系统的对外契约，Agent 只依赖本模块，不直接接触
知识卡片原始 JSON（卡片资产的加载/校验见 loader.py）。

【为什么契约单独成模块】
本文件不 import 任何 app.rag 内部模块（零内部依赖），因此上层
（app/agent、存档、离线评估脚本）引契约不会连带加载检索实现，
也不会形成 loader ← retriever ← agent 的循环导入。

【为什么用 pydantic 而不是 frozen dataclass】
loader.RagAssetReport / emergency_shadow 的报告是“进程内只读结构”，
用 frozen dataclass 最经济；而本模块的对象要跨边界：
写日志（extra 字段）、写对话存档（JSONL 与 PG 列）、必要时进 SSE。
用 BaseModel 可直接 model_dump / 序列化，且字段类型在构造时就被校验。

【契约稳定性（改名/删字段前必读）】
字段名与枚举取值已被下游当“字符串字面量”使用，属于线上协议：
- 日志事件 rag_result：decision/reason_codes/top_score/retrieval_ms；
- 对话存档列：rag_decision、rag_top_score（app/tasks/models.py）；
- 离线评估脚本按 reason_codes 聚合命中/拒答率。
因此字段重命名与枚举取值变更都是破坏性变更，需同步改上述写入方
（检索器 retriever.py / hybrid_retriever.py）与离线统计口径。
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
    - OUT_OF_SCOPE：超出知识库范围（预留，当前无生产路径写入）
    - UNAVAILABLE：知识资产未配置/校验失败/文件缺失，检索不可用
    - DISABLED：配置关闭 RAG（预留，当前无生产路径写入）

    【为什么用 StrEnum】
    既是枚举（可 is 比较），又天然是字符串（可直接进 JSON / 日志 / 存档 /
    前端，不必做 value 转换）；代码里写 .value 与直接使用等价。

    【消费方与常见分支】
    - ConsultAgent 阶段 6：仅 SUFFICIENT 时把 hits 投影为 grounded 证据、
      取 questions_to_ask 参与追问；其余状态一律不注入卡片；
    - 卡片直答（阶段 6.5）：要求 SUFFICIENT **且** is_fast_answerable 为真；
    - UNAVAILABLE 时上层会标记 knowledge_consult 降级但**继续问诊**，
      因此它不是错误码，而是“无知识库增强”的正常分支。
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

    【唯一写入方】
    只在检索器内部构造（retriever.py / hybrid_retriever.py 的 search），
    上层不会自行 new 一个 RagHit；两张检索器输出的字段含义必须一致。

    【字段来源】
    card_id / fact_ids / source_ids 均取自 loader 归一化后的卡片，
    其中 source_ids 已通过来源清单与版权校验（可对外溯源）。

    【注意 semantic_score 不是“模型相似度”】
    纯词法模式恒为 0.0；HybridRetriever **也**把它保持为 0.0，
    向量贡献只体现在 final_score 里。因此不要拿它做阈值判断。

    【final_score 的语义随检索器而变】
    词法 = 词面加权分；混合 = 词面 0.7 + 向量 0.3。
    所以 final_score 只在同一检索器内可比，不要跨模式共享阈值。
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

    【字段写入方对应关系（谁填、填什么）】
    ① query     ← search() 原样写入，不做归一化（便于与用户原话逐字对照）
    ② decision  ← search() 的“先门禁后阈值”判定结果（含一票否决的政策词覆盖）
    ③ reason_codes ← 分支固定码（vague_general_query / ambiguous_elimination /
                    cat_chin_specific_no_matching_card / low_relevance）
                    + UNAVAILABLE 时原样回传的资产错误码
    ④ hits      ← 评分循环产出，不变量：降序且长度 ≤ top_k
    ⑤ top_score ← hits[0].final_score；无命中为 None
                  （Optional 是为了区分“无命中”与“命中但 0 分”）
    ⑥ index_version ← loader 读到的资产版本；即使 UNAVAILABLE 也会带上，
                    否则无法判断是哪一版资产坏了
    ⑦ embedding_model_version ← 目前全项目无写入方，恒为默认占位值，
                    仅为将来接入真实模型版本标识预留
    ⑧ retrieval_ms ← 计时起点在 search() 最开始，覆盖所有早退分支，
                    保证监控口径可比
    """

    query: str            # 原始查询文本（用户本轮文字）
    decision: RagDecisionStatus   # 检索决策状态（见枚举说明）
    reason_codes: list[str] = Field(default_factory=list)   # 机器可读原因码（如 vague_general_query/low_relevance）
    hits: list[RagHit] = Field(default_factory=list)   # 命中卡片列表（按 final_score 降序，最多 top_k 张）
    top_score: float | None = None   # 最高分卡片的 final_score；无命中为 None
    index_version: str            # 知识库索引版本号（来自资产 validation_report）
    embedding_model_version: str = "none-lexical-shadow"  # 嵌入模型版本；纯词法 Shadow 模式为默认占位值
    retrieval_ms: float = 0.0   # 本次检索耗时（毫秒），用于监控
