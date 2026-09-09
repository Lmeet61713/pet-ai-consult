"""RAG（检索增强）包（app.rag）—— 知识卡片的加载、校验与检索。

【包职责】
把经过兽医审核流程的知识卡片资产（assets/rag/v1_x/）加载为只读索引，
在问诊流程中提供：
1. 知识卡片检索（词法 Shadow / 词法+向量混合），输出 RagResult；
2. 检索证据投影（build_grounded_evidence），把卡片内容白名单式地
   注入生成上下文；
3. 简单问答卡片直答（fast answer），高置信场景不调生成模型；
4. 急症规则影子匹配（emergency_shadow），在不影响线上分诊的前提下
   验证待发布急症规则资产。

【当前阶段定位】
知识资产处于"测试层（index_tier=test, production_eligible=False）"，
加载时强制校验发布闸门、内容哈希与审核队列一致性；检索结果只作为
 grounded 证据与追问参考，不替代医疗安全链路。

【模块组成】
- models.py：RagDecisionStatus / RagHit / RagResult 结果契约
- loader.py：RagAssetLoader 资产加载与多重校验（校验和/哈希/来源/审核状态）
- retriever.py：ShadowRetriever 轻量词法检索器（无向量依赖，可解释）
- hybrid_retriever.py：HybridRetriever 词面(0.7)+BGE-M3 向量(0.3)混合检索，
  模型不可用时自动回退纯词面
- emergency_shadow.py：V14EmergencyShadowMatcher 急症规则影子匹配器
"""

from app.rag.models import RagDecisionStatus, RagHit, RagResult

__all__ = ["RagDecisionStatus", "RagHit", "RagResult"]
