# RAG v1.4 / v1.5 状态

更新日期：2026-08-15

## 当前资产

目录：`assets/rag/v1_4/`（运行时默认）、`assets/rag/v1_5/`（修复版，可 Shadow）

| 项目 | 数量或状态 |
|---|---:|
| 知识卡 | 260（v1.5 已重写追问并修复红旗/就医指引） |
| 急症规则 | 40（v1.5 沿用 v1_4 内容） |
| 来源 | 116 |
| 类别 | 27 |
| 审核队列 | 300 条，全部 pending |
| production eligible | 0 |

v1.4 校验结果为 `shadow_ready=true`、`production_ready=false`、`errors=[]`。
v1.5 已于 2026-08-15 补齐资产集（审核队列、校验报告、SHA256SUMS、基准种子）并接入
loader/急症匹配器/校验与评测脚本，`validate_rag_assets.py --assets assets/rag/v1_5`
结果为 `shadow_ready=true`、`errors=[]`；shadow 评测种子回归通过（recall@1=1.0、物种错配 0）。

指向 v1_5 的方式：设置 `RAG_INDEX_PATH=assets/rag/v1_5`（Shadow 模式）。运行时默认仍为
v1_4，待 v1.5 修复复审（`review_v15_fixes.py`）无高危问题后再切换默认。

## 模式

- `off`：不加载 RAG。
- `shadow`：检索和急症匹配只写日志，不影响回答和分诊。
- `grounded`：只允许非生产验证；将受限卡片摘要传给生成模型。

生产启动当前强制要求 `RAG_MODE=off`。

## 已知限制

- 300 条记录尚无执业兽医签审。
- 311 条证据记录中有 309 条只定位到论文 Abstract。
- 平均每张卡约 1.2 个来源，证据厚度有限。
- 每张卡只有 2 条 `user_phrases`，真实口语覆盖不足。
- 当前检索器是中文二元字符、关键词和短语匹配，没有向量召回和 reranker。
- 780 条种子问题来自卡片自身标题和短语；满分结果只代表回归通过，不代表真实用户召回率。
- 当前 API 响应没有完整的用户可见引用字段。

## 发布门槛

1. 中国大陆执业兽医按内容哈希签审高风险规则和知识卡。
2. 使用至少 500 条、建议 1000 条真实脱敏问题做分层盲测。
3. 测量急症召回、物种正确率、不安全回答率和弃答准确率。
4. 增加 claim 级证据定位和最终回答引用。
5. 完成生产开关、回滚和版本审计后，才能改变 `production_eligible`。

详细真实性报告以 `assets/rag/v1_4/TRUTH_REPAIR_REPORT.md` 为唯一副本。

