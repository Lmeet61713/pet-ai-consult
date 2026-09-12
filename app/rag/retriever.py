"""
轻量词法 Shadow 检索器（Lexical Shadow Retriever）

使用可解释的短语/字符 n-gram 检索，暂不引入向量依赖。
适用于 100 张左右的知识卡片规模。

【在系统中的位置】
ConsultAgent 阶段 6 调用 search() 拿到 RagResult，再决定：
    1) decision 与 reason_codes → 知识库能不能回答这个问题；
    2) hits → 供 build_grounded_evidence() 投影为生成上下文；
    3) top 卡片的 questions_to_ask → 参与完整度追问查缺。
本模块是纯计算（无 IO、无模型调用），所以可以安全地在请求主链路上同步执行。

【为什么叫 “Shadow”】
相对于 hybrid_retriever.py 的向量混合检索，本检索器是“影子/基线”：
它不依赖任何外部模型，永远可用，既是混合检索的降级回退（模型不可用时的
最终退路），也是评估混合检索效果的对照基准。两者输出契约（RagResult）
完全一致，因此上层可在两者之间透明切换。

检索策略：
1. 查询词法化（CJK 分词 + 英文词干化）
2. 与知识卡片的多维度匹配（关键词、标题、短语、物种、分类）
3. 模糊查询过滤（含糊表达、歧义表达不触发检索）
4. 倾向性卡片提升（同类症状中优先推荐内容更完整的卡片）

安全设计：
- 药品相关术语（剂量、mg、处方等）→ POLICY_RESTRICTED，禁用知识卡片
- 含糊表达（"没精神""状态不好"）→ INSUFFICIENT，不检索专病卡片
- 歧义表达（"上厕所"未明确是排尿还是排便）→ INSUFFICIENT

【三类查询门禁的执行顺序（不可颠倒）】
资产可用 → 含糊 → 歧义 → 猫下巴专属 → 正常评分。
门禁必须在评分之前：一旦先算出 hit 再丢弃，既浪费算力，也容易被
上游误用（例如已经取走了追问清单）。因此宁可提前返回空结果。

【本模块对外提供三种产物】
- search()                  → RagResult（检索决策 + 命中）
- build_grounded_evidence() → 白名单投影后的模型上下文证据
- build_fast_answer()       → 卡片直答 payload（不调生成模型）
"""
from __future__ import annotations

import re
import time
from collections.abc import Iterable

from app.rag.loader import RagAssetReport
from app.rag.models import RagDecisionStatus, RagHit, RagResult

# 连续中文字符串匹配正则（用于 CJK 分词：整段 + 2-gram）
# 不用 jieba 等分词库：① 零依赖、无版本风险；② 词典不可知，避免术语更新后失效；
# 代价是会产生跨词边界的伪词，靠 _overlap 的重叠率与停用词表稀释噪声。
_CJK_RE = re.compile(r"[一-鿿]+")
# 英文/数字单词匹配正则（统一小写处理）
_WORD_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
# 政策限制词：用药剂量/处方类问题禁止用知识卡片回答 → POLICY_RESTRICTED
# 注意：匹配时对 query 做了 lower()，所以这里只需罗列小写形式（如 mg）。
# 维护提示：新增词请同时评估 hybrid_retriever 的影响（它直接 import 本常量）。
_POLICY_TERMS = ("剂量", "mg", "毫克", "停药", "减量", "换药", "处方", "疗程")

# “状态不好/没精神”只表达了非特异性全身状态，不能据此把任意包含
# “精神变差”的眼科、胃肠、皮肤等专病卡片注入生成上下文。
_VAGUE_GENERAL_TERMS = (
    "不舒服",
    "没精神",
    "没有精神",
    "精神不好",
    "精神不佳",
    "精神差",
    "精神萎靡",
    "精神沉郁",
    "蔫",
    "状态不好",
    "状态不佳",
    "状况不好",
    "状况不佳",
    "看起来不好",
    "看起来不太好",
    "看起来不是很好",
)

# 只要用户同时提供了明确症状/部位，就允许正常检索对应专病卡片。
_SPECIFIC_CLINICAL_TERMS = (
    "呕吐", "吐", "干呕", "腹泻", "拉稀", "便秘", "便血", "黑便",
    "不排便", "不吃", "拒食", "喝水", "流口水", "腹胀", "肚子", "腹痛",
    "打喷嚏", "喷嚏", "咳嗽", "流鼻涕", "鼻塞", "呼吸", "喘", "发热", "发烧",
    "眼", "流泪", "分泌物", "耳", "皮肤", "掉毛", "脱毛", "发红", "红肿",
    "瘙痒", "抓挠", "疼", "痛", "跛", "腿", "站不稳", "走不动", "抽搐",
    "尿", "排尿", "尿血", "血", "中毒", "误食", "异物", "外伤", "伤口",
)

# “上厕所/去厕所”在口语中可能同时指排尿和排便。未出现更明确的
# 尿液或粪便描述前，不让检索器靠通用词强行匹配某一张知识卡。
_AMBIGUOUS_ELIMINATION_TERMS = ("上厕所", "去厕所", "跑厕所", "如厕")
# 解除歧义的“尿液侧”证据词：命中任一即认为用户已明确指向排尿问题
_EXPLICIT_URINARY_TERMS = (
    "排尿", "小便", "撒尿", "尿尿", "尿频", "尿血", "尿不出", "尿量", "尿液",
)
# 解除歧义的“粪便侧”证据词：命中任一即认为用户已明确指向排便问题
_EXPLICIT_STOOL_TERMS = (
    "排便", "大便", "便便", "拉屎", "粪便", "拉稀", "腹泻", "便秘", "软便", "便血", "黑便",
)


class ShadowRetriever:
    """轻量词法 Shadow 检索器。

    基于关键词重叠、短语匹配、物种筛选和分类匹配的多维度评分检索。
    不依赖向量模型，全词法可解释。

    评分公式：
        final_score = keyword_score * 0.72 + title_score * 0.12
                    + phrase_score * 0.2 + species_score + category_score

    权重设计（调参前必读）：
        keyword 0.72 → 主信号：症状词必须与卡片检索文本真实重叠
        phrase  0.20 → 次信号：命中卡片预置的用户原话（最贴近真实表达）
        title   0.12 → 辅助信号：标题通常含病名，权重故意压低以避免“标题党”
        species 0.08 → 仅作加分，不能单独支撑候选（见 search() 内的准入判断）
        category 0.04 → 上游显式传入分类时才加分，用于轻微消歧
        （合计上限被 min(1.0, ...) 封顶，保证与阈值口径一致；
           hybrid_retriever 的 0.7/0.3 融合正是建立在这套权重之上）

    阈值说明：
        - threshold (0.24)：检索结果有效的最低分
          （取值经验：仅命中 1~2 个关键词 + 物种加分时约为 0.24，
            再高会开始漏掉真实但表达简短的问诊）
        - fast_threshold (0.55)：直答模式（无需模型生成）的置信度阈值
          （更严格：直答不经过生成模型的措辞缓冲，必须确保卡片高度对口）
    """

    def __init__(
        self,
        report: RagAssetReport,
        *,
        top_k: int = 4,
        threshold: float = 0.24,
        fast_threshold: float = 0.55,
    ):
        # 纯数据引用，不复制卡片：检索器与报告共享同一份只读资产
        self.report = report  # 知识卡片资产报告
        self.top_k = top_k  # 最大返回卡片数（过多会给生成模型引入噪声）
        self.threshold = threshold  # 有效匹配最低分
        self.fast_threshold = fast_threshold  # 直答模式阈值

    def search(self, query: str, *, species: str | None = None, category: str | None = None) -> RagResult:
        """执行知识卡片检索。

        处理流程：
        1. 资产不可用 → UNAVAILABLE
        2. 含糊查询（无明确症状）→ INSUFFICIENT
        3. 歧义查询（"上厕所"不明确）→ INSUFFICIENT
        4. 猫下巴特有查询（无匹配卡片）→ INSUFFICIENT
        5. 多维度评分检索（关键词 × 0.72 + 标题 × 0.12 + 短语 × 0.2 + 物种 + 分类）
        6. 倾向性卡片提升（promote_preferred_card）
        7. 政策限制检测（药品术语 → POLICY_RESTRICTED）

        注意：步骤 1~4 是“早退分支”，都返回 hits=[]。
        上游（ConsultAgent）对空 hits 的处理是“不注入知识库增强”，
        而不是报错，因此这些分支必须显式给出 reason_codes 以便观测。

        Args:
            query: 用户查询文本
            species: 物种限定（猫/狗/None）
            category: 分类限定

        Returns:
            RagResult 包含检索决策、命中列表和评分
        """
        # 计时覆盖所有早退分支，保证 retrieval_ms 在监控口径上可比
        started = time.perf_counter()
        if not self.report.ready:
            # 资产未通过加载校验：把加载期的错误码原样回传，
            # 这样“为什么检索不可用”可以直接在检索结果里看到，无需翻日志
            return RagResult(
                query=query,
                decision=RagDecisionStatus.UNAVAILABLE,
                reason_codes=list(self.report.errors) or ["asset_unavailable"],
                index_version=self.report.index_version,
                retrieval_ms=(time.perf_counter() - started) * 1000,
            )

        # 门禁 1：只有“非特异性状态描述”而没有症状/部位词 → 不检索。
        # 理由：这类词（没精神/状态不好）几乎能匹配到所有含对应表述的专病卡片，
        # 一旦注入生成上下文，模型就会围绕错误的病种组织回答。
        if is_vague_general_query(query):
            return RagResult(
                query=query,
                decision=RagDecisionStatus.INSUFFICIENT,
                reason_codes=["vague_general_query"],
                hits=[],
                top_score=None,
                index_version=self.report.index_version,
                retrieval_ms=(time.perf_counter() - started) * 1000,
            )

        # 门禁 2：“上厕所”未说明排尿还是排便 → 不检索。
        # 泌尿与消化道是两套完全不同的卡片，猜错会直接给出错误方向的建议。
        if is_ambiguous_elimination_query(query):
            return RagResult(
                query=query,
                decision=RagDecisionStatus.INSUFFICIENT,
                reason_codes=["ambiguous_elimination"],
                hits=[],
                top_score=None,
                index_version=self.report.index_version,
                retrieval_ms=(time.perf_counter() - started) * 1000,
            )

        # 门禁 3：猫下巴皮损是一类高精度识别场景——现有资产中没有对口卡片，
        # 而词面检索极易被“下巴+掉毛”拉到腰背/尾根跳蚤皮炎卡片上（错病种）。
        # 因此宁可返回 INSUFFICIENT（由完整度检查器追问），也不给错卡片。
        if is_cat_chin_specific_query(query, species):
            return RagResult(
                query=query,
                decision=RagDecisionStatus.INSUFFICIENT,
                reason_codes=["cat_chin_specific_no_matching_card"],
                hits=[],
                top_score=None,
                index_version=self.report.index_version,
                retrieval_ms=(time.perf_counter() - started) * 1000,
            )

        # 物种归一（中文/英文变体 → cat/dog/None）与查询词表（已去停用词）
        normalized_species = normalize_species(species)
        query_terms = _query_terms(query)
        # 三元组：(final_score, keyword_score, card)
        # 保存 keyword_score 是为了写入 RagHit.keyword_score，便于线上区分
        # “真词面命中”与“靠短语/物种加分凑上去”的命中。
        ranked: list[tuple[float, float, dict]] = []
        for card in self.report.cards:
            # 卡片声明的物种集合（可能多物种，如猫犬通用卡）
            card_species = {
                value
                for value in (
                    normalize_species(item) for item in card.get("species", [])
                )
                if value
            }
            if normalized_species:
                # 已知物种：卡片必须显式包含该物种
                if normalized_species not in card_species:
                    continue
            elif not {"cat", "dog"}.issubset(card_species):
                # 未知物种只能使用猫狗通用卡片，禁止随机落到单一物种。
                # 判据：卡片必须同时声明 cat 与 dog（而非“至少一个”）。
                continue
            card_text = str(card.get("retrieval_text", ""))
            card_terms = _terms(card_text)
            # 四个子分：关键词重叠 / 标题重叠 / 用户短语命中 / 物种与分类加分
            keyword_score = _overlap(query_terms, card_terms)
            title_score = _overlap(query_terms, _terms(str(card.get("title", ""))))
            phrase_score = _phrase_score(query, card.get("user_phrases", []))
            species_score = 0.08 if normalized_species else 0.0
            category_score = 0.04 if category and category == card.get("category") else 0.0
            # 物种本身不是病症相关性；至少命中关键词、短语或明确分类才进入候选。
            # 否则“猫”这一个字就能让所有猫卡带上 0.08 分进入候选池。
            if keyword_score <= 0 and phrase_score <= 0 and category_score <= 0:
                continue
            # 绝对上限 1.0：保证阈值（0.24/0.32/0.55）在统一尺度上可比，
            # 也保证 hybrid_retriever 的 fix = alpha*1.0 + (1-alpha)*1.0 不超过 1。
            final_score = min(
                1.0,
                keyword_score * 0.72
                + title_score * 0.12
                + phrase_score * 0.2
                + species_score
                + category_score,
            )
            if final_score > 0:
                ranked.append((final_score, keyword_score, card))
        # 排序键：分数降序 + 卡片 ID 升序。
        # 加 ID 作为第二键是为了消除排序不确定性（同分卡片顺序稳定可复现）。
        ranked.sort(key=lambda item: (-item[0], item[2].get("id", "")))
        # 倾向性提升：把特定场景的专属卡提到首位（不改分，只改顺序）
        ranked = promote_preferred_card(ranked, query, normalized_species)
        # 只保留 top_k：下游会把命中卡片投影进 prompt，过多会稀释要点
        selected = ranked[: self.top_k]
        hits = [
            RagHit(
                card_id=card["id"],
                # 事实 ID 全量携带：允许上层/离线评估定位到具体事实
                fact_ids=[fact["id"] for fact in card.get("facts", [])],
                # 来源去重排序：用于回复中的可读引用，也保证同一结果可重复
                source_ids=sorted(
                    {
                        ref["source_id"]
                        for fact in card.get("facts", [])
                        for ref in fact.get("evidence_refs", [])
                        if ref.get("source_id")
                    }
                ),
                # 纯词法模式无向量分，但字段必须写入，保证契约与混合检索一致
                semantic_score=0.0,
                keyword_score=keyword_score,
                final_score=final_score,
            )
            for final_score, keyword_score, card in selected
        ]
        # top_score 取“实际返回的列表”里的最大值（提升后顺序不变，因此等价于首位）
        top_score = max((hit.final_score for hit in hits), default=None)
        reasons: list[str] = []
        if not hits or (top_score is not None and top_score < self.threshold):
            # 无命中或最高分不过线：一律 INSUFFICIENT，不向生成方注入卡片
            decision = RagDecisionStatus.INSUFFICIENT
            reasons.append("low_relevance")
        else:
            decision = RagDecisionStatus.SUFFICIENT
        # 政策限制词优先级最高：即使已经 SUFFICIENT 也要被覆盖为
        # POLICY_RESTRICTED（剂量/处方类问题必须交给安全链路，而不是知识卡片）。
        # 位置刻意放在阈值判定之后，形成“一票否决”效果。
        if any(term in query.lower() for term in _POLICY_TERMS):
            decision = RagDecisionStatus.POLICY_RESTRICTED
            reasons.append("policy_restricted_term")
        # 注意：POLICY_RESTRICTED/INSUFFICIENT 时仍返回 hits（便于观测与归因），
        # 但下游 build_grounded_evidence() 会因 decision 不是 SUFFICIENT 而返回空，
        # 因此这些 hits 不会被注入生成上下文。
        return RagResult(
            query=query,
            decision=decision,
            reason_codes=reasons,
            hits=hits,
            top_score=top_score,
            index_version=self.report.index_version,
            retrieval_ms=(time.perf_counter() - started) * 1000,
        )

    def build_grounded_evidence(self, result: RagResult) -> list[dict]:
        """将检索结果映射为安全、精简的模型上下文载荷。

        原始卡片包含来源和审核元数据，模型不需要这些信息。
        该投影防止任意资产字段变成 prompt 中的指令，并保持上下文大小可控。

        【为什么必须是“白名单投影”】
        卡片资产是外部审核产物，字段内容可被内容编辑者改写。若整卡塞进 prompt，
        卡片里的任何文本都可能被模型当作指令执行（提示注入），
        因此这里只挑选下面 6 个字段，并逐一截断长度。

        【为什么只取前 2 张卡】
        实测 top1 已覆盖绝大多数有效场景，top2 用于补全交叉信息（如猫犬差异）；
        再往后排的卡片相关度下降明显，加入后反而会稀释要点。

        Args:
            result: 检索结果

        Returns:
            精简后的证据列表（最多 2 张卡片，每张卡片最多 4 个事实）
        """
        # 非 SUFFICIENT 一律不投影：这是 POLICY_RESTRICTED/INSUFFICIENT/资产
        # 不可用等情况下“卡片内容绝不进 prompt”的强制执行点。
        if result.decision is not RagDecisionStatus.SUFFICIENT:
            return []
        cards_by_id = {card["id"]: card for card in self.report.cards}
        evidence: list[dict] = []
        for hit in result.hits[:2]:
            card = cards_by_id.get(hit.card_id)
            if card is None:
                # 理论上不会发生（hits 来自同一份 cards）；防御性跳过，避免脏资产导致崩溃
                continue
            evidence.append(
                {
                    "card_id": card["id"],
                    # 标题限 120 字符：超过这个长度的标题已不可能是“标题”
                    "title": str(card.get("title", ""))[:120],
                    # 物种只取前 2 个：卡片可能声明多物种，长列表对模型无信息增量
                    "species": list(card.get("species", []))[:2],
                    # 按“与本次问题相关度”挑事实，而非固定取前几条
                    "supported_facts": select_supported_facts(
                        card, result.query, limit=4
                    ),
                    # 安全建议限 240 字符：保留完整一句话，防止被截成无效指令
                    "safe_next_step": str(card.get("safe_next_step", ""))[:240],
                    # 红旗信号限 5 条、每条 100 字符：红旗用于风险提示，需简短可扫
                    "red_flags": [str(item)[:100] for item in card.get("red_flags", [])[:5]],
                }
            )
        return evidence

    # ------------------------------------------------------------ 直答通道

    def is_fast_answerable(self, result: RagResult) -> bool:
        """实例方法包装：判断检索结果是否满足卡片直答条件（使用实例阈值）。

        注意这里用 self.fast_threshold（默认 0.55），而模块级
        is_fast_answerable() 的默认值是 0.4，两者不一致是有意的：
        模块级函数服务于离线评估/外部调用（口径宽松），实例方法服务于线上直答
        （口径更严）。改阈值时需同时确认两条路径。
        """
        return is_fast_answerable(self.report, result, fast_threshold=self.fast_threshold)

    def build_fast_answer(self, result: RagResult) -> dict:
        """实例方法包装：把命中卡片渲染为直答 payload（不调生成模型）。

        未传 query_species，因此直答文案不做猫/犬措辞替换；
        需要物种替换请直接调模块级 build_fast_answer(report, result, query_species)。
        """
        return build_fast_answer(self.report, result)


def _terms(text: str) -> set[str]:
    """将文本词法化为搜索词集合。

    处理 CJK 字符（2-gram 分词）和英文单词，统一小写。

    【为什么同时加入“整段”与“2-gram”】
    - 整段：让“慢性肾衰”这类固定术语作为强特征参与重叠（命中时贡献大）；
    - 2-gram：中文无空格，靠 2-gram 才能让“猫咪呕吐”与“猫呕吐”部分重叠。
    代价是产生大量跨词边界的伪词，因此仅在查询侧用 _QUERY_STOPWORDS 稀释噪声。

    :param text: 原文（中英文混排均可）
    :return: 小写词集合（已去除空串）
    """
    terms: set[str] = set()
    # 连续中文被视为“一段”（_CJK_RE 贪婪匹配），标点/空格/字母即为段边界
    for group in _CJK_RE.findall(text.lower()):
        terms.add(group)
        # range(len-1)：单字（长度 1）不会产生 2-gram，只保留整段
        terms.update(group[index : index + 2] for index in range(len(group) - 1))
    terms.update(_WORD_RE.findall(text.lower()))
    # 过滤空串：极端输入（如全空白）可能产生空元素，空词会让重叠率虚高
    return {term for term in terms if term}


def _overlap(query_terms: Iterable[str], card_terms: set[str]) -> float:
    """计算查询词与卡片词的重叠率（Jaccard-like）。

    分母只取查询侧词数（不是并集），因此对“卡片文本很长”不敏感：
    长卡片不会因为词多而被系统性压低分数——这正是关键词分能作为
    （权重 0.72 的）主信号的前提。

    :return: 0~1；查询为空时返回 0（靠 max(len, 1) 避免除零）
    """
    query = set(query_terms)
    return len(query & card_terms) / max(len(query), 1)


# 查询侧通用词: 不携带病症信息, 只会稀释信号词权重并制造跨卡命中
#
# 表内有两类词，都必须保留：
#   ① 完整问句词：怎么办 / 什么原因 / 需要注意什么 —— 它们不是症状；
#   ② 2-gram 碎片：么办 / 要注 / 意什 —— ①被切分后的残留，同样不携带病症信息。
# 漏掉①的碎片会导致“怎么办”这类高频问句在每张卡上都命中一个词片，
# 从而把真正的症状信号（如“呕吐”）稀释掉。
_QUERY_STOPWORDS = {
    "怎么", "么办", "怎么办", "什么", "为什么", "多少", "怎样", "怎么样",
    "是不是", "可以", "需要", "应该", "请问", "如何", "是否", "要紧",
    "咋办", "咋", "回事", "好吗", "行吗", "能行",
    "猫咪", "猫猫", "狗狗", "小狗", "小狗狗", "宠物", "偶尔", "经常", "总是", "今天", "目前", "现在",
    "原因", "什么原因",
    "最近", "要注", "注意", "意什", "需要注意什么",
}


def _query_terms(text: str) -> set[str]:
    """查询词集合: 过滤通用问句词, 避免"怎么办"类词稀释病症信号。"""
    # 只在查询侧过停用词：卡片侧保持资产原文（资产已由兽医审核，
    # 不应因为检索器的词表而改变其语义）。
    return _terms(text) - _QUERY_STOPWORDS


def _phrase_score(query: str, phrases: list[str]) -> float:
    """计算查询与卡片用户短语的匹配度。

    用户短语是从真实用户问题中提炼的典型表达方式，
    匹配度越高说明卡片越贴近用户的实际问题场景。

    与关键词分的关键区别：这里是“整短语子串包含”而非词集合重叠，
    因此能吃到语序与搭配信息（如“一直吧唧嘴”连在一起才算命中），
    代价是容易过拟合到具体措辞，故权重（0.2）低于关键词分。

    :return: 命中短语数 / 短语总数，封顶 1.0；无短语时为 0
    """
    if not phrases:
        return 0.0
    lowered = query.lower()
    # 除以 len(phrases)：短语多的卡片不应仅因为“试得多”而占便宜
    return min(1.0, sum(1 for phrase in phrases if phrase.lower() in lowered) / len(phrases))


def is_vague_general_query(query: str) -> bool:
    """识别只有非特异性状态描述、没有明确症状或身体部位的查询。

    判定式 = 命中含糊词 AND 未命中任何具体临床词：
    “没精神，还呕吐”因带具体症状而放行正常检索；
    仅“没精神”则拦截（否则会把各类专病卡片都拉进来）。

    先去除全部空白字符，防止“没 精神”这类输入绕过子串匹配。
    """
    normalized = re.sub(r"\s+", "", (query or "").lower())
    if not normalized:
        # 空查询不是“含糊”，返回 False 让上游按其他逻辑处理
        return False
    has_vague_signal = any(term in normalized for term in _VAGUE_GENERAL_TERMS)
    has_specific_signal = any(term in normalized for term in _SPECIFIC_CLINICAL_TERMS)
    return has_vague_signal and not has_specific_signal


def is_ambiguous_elimination_query(query: str) -> bool:
    """识别未说明排尿还是排便的口语化“上厕所”问题。

    判定式 = 命中歧义词 AND 未命中任何“尿液侧/粪便侧”明确证据词。
    两个词表都在模块顶部，新增同义词时两侧都要考虑（否则新说法又会被判为歧义）。
    """
    normalized = re.sub(r"\s+", "", (query or "").lower())
    if not any(term in normalized for term in _AMBIGUOUS_ELIMINATION_TERMS):
        return False
    explicit = _EXPLICIT_URINARY_TERMS + _EXPLICIT_STOOL_TERMS
    return not any(term in normalized for term in explicit)


def is_cat_chin_specific_query(query: str, species: str | None = None) -> bool:
    """高精度识别猫下巴皮损，避免误注入腰背/尾根跳蚤皮炎卡片。

    三个条件必须同时成立（高门槛思路：宁可漏拦，不可错拦）：
      ① 猫语境：显式物种为 cat，或文本出现“猫”；
      ② 部位：出现“下巴/颏部”；
      ③ 皮损表现：黑色颗粒/黑点/黑头/粉刺/结痂/掉毛/脱毛。
    只命中“猫 + 掉毛”不拦（可能是其他部位），以免妨碍正常检索。
    """
    normalized = re.sub(r"\s+", "", (query or "").lower())
    normalized_species = normalize_species(species)
    cat_context = normalized_species == "cat" or "猫" in normalized
    chin_context = "下巴" in normalized or "颏部" in normalized
    lesion_context = any(
        term in normalized
        for term in ("黑色颗粒", "黑点", "黑头", "粉刺", "结痂", "掉毛", "脱毛")
    )
    return cat_context and chin_context and lesion_context


def preferred_card_id(query: str, species: str | None = None) -> str | None:
    """高精度日程问题优先使用信息完整的专属卡，避免泛化驱虫卡排在前面。

    【为什么需要硬编码卡片 ID】
    纯词面评分只度量“文本相似”，无法区分“同一主题下哪张卡信息更完整”。
    如“幼猫驱虫”这类问题，泛化驱虫卡与幼猫专属卡词面分接近，
    但后者才含月龄对应的日程表，因此这里用场景规则直接指定。

    【维护约束（改资产时必看）】
    下方 ID 与资产生命周期绑定：卡片改名/下线时必须同步修改本函数，
    否则覆盖会静默失效（promote_preferred_card 找不到 ID 时直接返回原序，
    不报错），表现为回答质量下降而无异常日志。

    :return: 命中场景时返回卡片 ID；无匹配返回 None（调用方保持原序）
    """
    normalized = re.sub(r"\s+", "", (query or "").lower())
    # 场景 1：猫 + 幼猫 + 日程类词（驱虫/除虫/疫苗/免疫）→ 幼猫专属卡
    cat_context = normalize_species(species) == "cat" or "猫" in normalized
    kitten_context = any(term in normalized for term in ("幼猫", "小猫", "奶猫"))
    schedule_context = any(term in normalized for term in ("驱虫", "除虫", "疫苗", "免疫"))
    if cat_context and kitten_context and schedule_context:
        return "V17-CAT-PED-001"
    # 场景 2：狗 + 明确排尿侧证据词（复用歧义词表，保证口径一致）→ 泌尿专属卡
    dog_context = normalize_species(species) == "dog" or any(
        term in normalized for term in ("狗", "犬")
    )
    if dog_context and any(term in normalized for term in _EXPLICIT_URINARY_TERMS):
        return "MVP-UR-001"
    return None


def promote_preferred_card(
    ranked: list[tuple[float, float, dict]], query: str, species: str | None = None
) -> list[tuple[float, float, dict]]:
    """把 preferred_card_id() 指定的卡片提到首位（仅调顺序，不改分数）。

    设计要点：
    - 只提升顺序，不动分数——因此 top_score 与阈值判定不受影响，
      避免“硬编码覆盖”意外把不过线的结果变成 SUFFICIENT；
    - 指定卡不在候选列表时不报错、返回原序（可能是被物种/准入过滤了）；
    - 保持其余卡片的相对顺序（列表推导而非重排序）。

    :return: 新列表（首位为目标卡片，其余按原序）；无目标卡时返回原列表
    """
    card_id = preferred_card_id(query, species)
    if not card_id:
        return ranked
    preferred = [item for item in ranked if item[2].get("id") == card_id]
    if not preferred:
        return ranked
    # preferred 最多只有 1 个元素（卡片 ID 唯一），+ 其余卡保持原序
    return preferred + [item for item in ranked if item[2].get("id") != card_id]


def select_supported_facts(card: dict, query: str, *, limit: int = 4) -> list[str]:
    """优先投影与本次问题最相关的事实，避免固定前3条漏掉驱虫等后半部分。

    排序键：(-相关度, 原始下标)：相关度由事实文本与查询的词面重叠率度量；
    相关度相同时按资产原始顺序（保证可复现，也尊重资产编辑者的排序意图）。

    :param card: 归一化后的卡片字典（含 source_supported_simple_facts）
    :param limit: 最多返回的事实数
    :return: 事实文本列表（每条截断到 180 字符）
    """
    facts = [str(item)[:180] for item in card.get("source_supported_simple_facts", []) if item]
    query_terms = _query_terms(query)
    ranked = sorted(
        enumerate(facts),
        key=lambda item: (-_overlap(query_terms, _terms(item[1])), item[0]),
    )
    return [fact for _, fact in ranked[:limit]]


def is_fast_answerable(
    report: RagAssetReport, result: RagResult, *, fast_threshold: float = 0.4
) -> bool:
    """简单问答直答准入：高置信命中简单问答/健康宣教卡片。

    条件：SUFFICIENT + top_score ≥ fast_threshold + 命中卡片 scope 属于
    simple_owner_question 或 common_disease_health_education*（健康宣教）。
    这类卡片内容为通用事实与安全建议，可直接渲染；症状分诊类卡片不直答。

    【为什么用 scope 而不是分数做二次闸门】
    分数只说明“像”，不说明“能直答”：分诊卡即使匹配度很高，
    也需要完整度追问与模型推理，跳过模型会静默丢掉该问的关键信息。

    【注意默认阈值 0.4 与实例阈值的差异】
    本函数默认 0.4（供离线评估/外部调用），而 ShadowRetriever 与
    HybridRetriever 的实例方法传入 self.fast_threshold（默认 0.55，更严）。
    """
    if result.decision is not RagDecisionStatus.SUFFICIENT:
        return False
    # top_score 为 None 表示无命中；低于阈值则不入直答通道
    if result.top_score is None or result.top_score < fast_threshold:
        return False
    if not result.hits:
        return False
    cards_by_id = {card["id"]: card for card in report.cards}
    # 只看首位命中（hits 已按分降序）：直答只能由 top1 卡片驱动
    top_card = cards_by_id.get(result.hits[0].card_id)
    if not top_card:
        return False
    scope = str(top_card.get("scope", ""))
    # 精确匹配：只放行"纯简单问答/健康宣教"卡片；
    # 不放行 common_disease_health_education_and_triage（分诊类需走追问，2026-08-19 修复）
    return scope == "simple_owner_question" or scope == "common_disease_health_education"


_SPECIES_WORD_MAP: dict[str, list[tuple[str, str]]] = {
    # query 物种 -> [(卡片措辞, 替换为), ...]（先长词后短词）
    # 顺序不能乱：必须先把“狗狗”替掉，否则先替“狗”会让“狗狗”变成“猫猫”。
    # 同理 dog 侧必须先把“猫咪”替掉，否则“猫咪”会变成“犬咪”。
    "cat": [("狗狗", "猫咪"), ("犬", "猫"), ("狗", "猫")],
    "dog": [("猫咪", "狗狗"), ("猫", "犬")],
}


def _adapt_species_text(text: str, query_species: str | None) -> str:
    """直答渲染时按提问物种替换卡片措辞（v1.5：修"问猫答犬"类问题）。

    仅用于直答通道：卡片资产里的猫/犬措辞无法事先枚举所有物种组合，
    因此在渲染时做后置替换；生成模型路径不需要此函数（模型会自行组织措辞）。

    :param query_species: 提问物种（cat/dog）；None 或其他值则原文返回
    """
    if not query_species or not text:
        return text
    for src, dst in _SPECIES_WORD_MAP.get(query_species, []):
        text = text.replace(src, dst)
    return text


def build_fast_answer(report: RagAssetReport, result: RagResult, query_species: str | None = None) -> dict:
    """把命中卡片渲染成直答 payload（不调生成模型）。

    返回 dict 供 agent 组装 GeneratedConsultation：summary 用卡片标题与
    事实，what_to_do_now 用 safe_next_step，追问用 questions_to_ask。

    【payload 字段与回答的对应关系（改结构时必看）】
        card_id             → 用于遥测/归因，说明本条回答来自哪张卡
        summary             → 直接作为回答正文（标题 + 最多 2 条事实）
        what_to_do_now      → 回答里的“可以先这样做”
        follow_up_questions → 追问（截前 2 条）
        possible_explanations / avoid_actions / what_to_monitor
                            → 固定空列表：直答不做推断，不编造病因与禁忌

    【为什么只取 2 条事实】
    直答不经过生成模型压缩，越多内容越容易变成“照搬说明书”，
    且会撑爆前端气泡；保留 2 条足够回答“是什么、怎么做”。

    :param query_species: 提问物种，用于把卡片里的猫/犬措辞换成与提问一致
    """
    cards_by_id = {card["id"]: card for card in report.cards}
    # 只取首位命中；缺失时用空字典，后续 get 全部落到默认值（不抛异常）
    card = cards_by_id.get(result.hits[0].card_id, {})
    facts = [str(item)[:120] for item in card.get("source_supported_simple_facts", [])][:2]
    title = str(card.get("title", ""))[:80]
    # 物种适配必须在拼装前完成：summary 与追问都依赖替换后的文本
    if query_species:
        title = _adapt_species_text(title, query_species)
        facts = [_adapt_species_text(f, query_species) for f in facts]
    summary_parts = [title]
    summary_parts.extend(facts)
    # 安全建议保留 240 字符：太长会推高阅读成本，太短会截断关键在于行动
    safe_next_step = _adapt_species_text(
        str(card.get("safe_next_step", ""))[:240], query_species
    )
    questions = [
        _adapt_species_text(str(q)[:100], query_species)
        for q in card.get("questions_to_ask", [])
    ][:2]
    return {
        "card_id": card.get("id", ""),
        # “；”拼接并过滤空项：避免出现“；；”或首尾多余分隔符
        "summary": "；".join(p for p in summary_parts if p),
        "what_to_do_now": [safe_next_step] if safe_next_step else [],
        "follow_up_questions": questions,
        "possible_explanations": [],
        "avoid_actions": [],
        "what_to_monitor": [],
    }


def normalize_species(species: str | None) -> str | None:
    """将物种描述标准化为 "cat"、"dog" 或 None。

    支持中文常见变体（猫咪、幼猫、老年犬等）和英文（feline/canine）。
    无法识别的物种返回 None，表示未知物种。

    【判定顺序是有意的】
    先判 cat、再判 dog，且每支都带子串回退（"猫" in lowered / "犬"/"狗"）。
    因此同时包含猫与狗的文本（如"猫狗双全"）会返回 "cat"。
    这在问诊场景下是可接受的降级：歧义由多宠/物种冲突检测（上层）负责，
    本函数只保证输出范围受限（cat/dog/None），不承担消歧职责。

    【被多处复用，改动需回归】
    loader/retriever/hybrid_retriever/emergency_shadow 与 ConsultAgent 的
    物种推断都依赖本函数，修改归一化规则会影响检索过滤、风险规则与追问。
    """
    if not species:
        return None
    lowered = species.strip().lower()
    # 注意到这里用“枚举 or 子串”双路径：既支持“feline”这类标准值，
    # 也支持“英短猫咪”“中华田园犬”这类带前缀后缀的自由描述。
    if lowered in {"cat", "feline", "猫", "猫咪", "幼猫", "老年猫"} or "猫" in lowered:
        return "cat"
    if lowered in {"dog", "canine", "犬", "狗", "狗狗", "幼犬", "老年犬"} or any(
        marker in lowered for marker in ("犬", "狗")
    ):
        return "dog"
    return None