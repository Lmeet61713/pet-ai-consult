"""
轻量词法 Shadow 检索器（Lexical Shadow Retriever）

使用可解释的短语/字符 n-gram 检索，暂不引入向量依赖。
适用于 100 张左右的知识卡片规模。

检索策略：
1. 查询词法化（CJK 分词 + 英文词干化）
2. 与知识卡片的多维度匹配（关键词、标题、短语、物种、分类）
3. 模糊查询过滤（含糊表达、歧义表达不触发检索）
4. 倾向性卡片提升（同类症状中优先推荐内容更完整的卡片）

安全设计：
- 药品相关术语（剂量、mg、处方等）→ POLICY_RESTRICTED，禁用知识卡片
- 含糊表达（"没精神""状态不好"）→ INSUFFICIENT，不检索专病卡片
- 歧义表达（"上厕所"未明确是排尿还是排便）→ INSUFFICIENT
"""
from __future__ import annotations

import re
import time
from collections.abc import Iterable

from app.rag.loader import RagAssetReport
from app.rag.models import RagDecisionStatus, RagHit, RagResult

# 连续中文字符串匹配正则（用于 CJK 分词：整段 + 2-gram）
_CJK_RE = re.compile(r"[一-鿿]+")
# 英文/数字单词匹配正则（统一小写处理）
_WORD_RE = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
# 政策限制词：用药剂量/处方类问题禁止用知识卡片回答 → POLICY_RESTRICTED
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
_EXPLICIT_URINARY_TERMS = (
    "排尿", "小便", "撒尿", "尿尿", "尿频", "尿血", "尿不出", "尿量", "尿液",
)
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

    阈值说明：
        - threshold (0.24)：检索结果有效的最低分
        - fast_threshold (0.55)：直答模式（无需模型生成）的置信度阈值
    """

    def __init__(
        self,
        report: RagAssetReport,
        *,
        top_k: int = 4,
        threshold: float = 0.24,
        fast_threshold: float = 0.55,
    ):
        self.report = report  # 知识卡片资产报告
        self.top_k = top_k  # 最大返回卡片数
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

        Args:
            query: 用户查询文本
            species: 物种限定（猫/狗/None）
            category: 分类限定

        Returns:
            RagResult 包含检索决策、命中列表和评分
        """
        started = time.perf_counter()
        if not self.report.ready:
            return RagResult(
                query=query,
                decision=RagDecisionStatus.UNAVAILABLE,
                reason_codes=list(self.report.errors) or ["asset_unavailable"],
                index_version=self.report.index_version,
                retrieval_ms=(time.perf_counter() - started) * 1000,
            )

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

        normalized_species = normalize_species(species)
        query_terms = _query_terms(query)
        ranked: list[tuple[float, float, dict]] = []
        for card in self.report.cards:
            card_species = {
                value
                for value in (
                    normalize_species(item) for item in card.get("species", [])
                )
                if value
            }
            if normalized_species:
                if normalized_species not in card_species:
                    continue
            elif not {"cat", "dog"}.issubset(card_species):
                # 未知物种只能使用猫狗通用卡片，禁止随机落到单一物种。
                continue
            card_text = str(card.get("retrieval_text", ""))
            card_terms = _terms(card_text)
            keyword_score = _overlap(query_terms, card_terms)
            title_score = _overlap(query_terms, _terms(str(card.get("title", ""))))
            phrase_score = _phrase_score(query, card.get("user_phrases", []))
            species_score = 0.08 if normalized_species else 0.0
            category_score = 0.04 if category and category == card.get("category") else 0.0
            # 物种本身不是病症相关性；至少命中关键词、短语或明确分类才进入候选。
            if keyword_score <= 0 and phrase_score <= 0 and category_score <= 0:
                continue
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
        ranked.sort(key=lambda item: (-item[0], item[2].get("id", "")))
        ranked = promote_preferred_card(ranked, query, normalized_species)
        selected = ranked[: self.top_k]
        hits = [
            RagHit(
                card_id=card["id"],
                fact_ids=[fact["id"] for fact in card.get("facts", [])],
                source_ids=sorted(
                    {
                        ref["source_id"]
                        for fact in card.get("facts", [])
                        for ref in fact.get("evidence_refs", [])
                        if ref.get("source_id")
                    }
                ),
                semantic_score=0.0,
                keyword_score=keyword_score,
                final_score=final_score,
            )
            for final_score, keyword_score, card in selected
        ]
        top_score = max((hit.final_score for hit in hits), default=None)
        reasons: list[str] = []
        if not hits or (top_score is not None and top_score < self.threshold):
            decision = RagDecisionStatus.INSUFFICIENT
            reasons.append("low_relevance")
        else:
            decision = RagDecisionStatus.SUFFICIENT
        if any(term in query.lower() for term in _POLICY_TERMS):
            decision = RagDecisionStatus.POLICY_RESTRICTED
            reasons.append("policy_restricted_term")
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

        Args:
            result: 检索结果

        Returns:
            精简后的证据列表（最多 2 张卡片，每张卡片最多 4 个事实）
        """
        if result.decision is not RagDecisionStatus.SUFFICIENT:
            return []
        cards_by_id = {card["id"]: card for card in self.report.cards}
        evidence: list[dict] = []
        for hit in result.hits[:2]:
            card = cards_by_id.get(hit.card_id)
            if card is None:
                continue
            evidence.append(
                {
                    "card_id": card["id"],
                    "title": str(card.get("title", ""))[:120],
                    "species": list(card.get("species", []))[:2],
                    "supported_facts": select_supported_facts(
                        card, result.query, limit=4
                    ),
                    "safe_next_step": str(card.get("safe_next_step", ""))[:240],
                    "red_flags": [str(item)[:100] for item in card.get("red_flags", [])[:5]],
                }
            )
        return evidence

    # ------------------------------------------------------------ 直答通道

    def is_fast_answerable(self, result: RagResult) -> bool:
        """实例方法包装：判断检索结果是否满足卡片直答条件（使用实例阈值）。"""
        return is_fast_answerable(self.report, result, fast_threshold=self.fast_threshold)

    def build_fast_answer(self, result: RagResult) -> dict:
        """实例方法包装：把命中卡片渲染为直答 payload（不调生成模型）。"""
        return build_fast_answer(self.report, result)


def _terms(text: str) -> set[str]:
    """将文本词法化为搜索词集合。

    处理 CJK 字符（2-gram 分词）和英文单词，统一小写。
    """
    terms: set[str] = set()
    for group in _CJK_RE.findall(text.lower()):
        terms.add(group)
        terms.update(group[index : index + 2] for index in range(len(group) - 1))
    terms.update(_WORD_RE.findall(text.lower()))
    return {term for term in terms if term}


def _overlap(query_terms: Iterable[str], card_terms: set[str]) -> float:
    """计算查询词与卡片词的重叠率（Jaccard-like）。"""
    query = set(query_terms)
    return len(query & card_terms) / max(len(query), 1)


# 查询侧通用词: 不携带病症信息, 只会稀释信号词权重并制造跨卡命中
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
    return _terms(text) - _QUERY_STOPWORDS


def _phrase_score(query: str, phrases: list[str]) -> float:
    """计算查询与卡片用户短语的匹配度。

    用户短语是从真实用户问题中提炼的典型表达方式，
    匹配度越高说明卡片越贴近用户的实际问题场景。
    """
    if not phrases:
        return 0.0
    lowered = query.lower()
    return min(1.0, sum(1 for phrase in phrases if phrase.lower() in lowered) / len(phrases))


def is_vague_general_query(query: str) -> bool:
    """识别只有非特异性状态描述、没有明确症状或身体部位的查询。"""
    normalized = re.sub(r"\s+", "", (query or "").lower())
    if not normalized:
        return False
    has_vague_signal = any(term in normalized for term in _VAGUE_GENERAL_TERMS)
    has_specific_signal = any(term in normalized for term in _SPECIFIC_CLINICAL_TERMS)
    return has_vague_signal and not has_specific_signal


def is_ambiguous_elimination_query(query: str) -> bool:
    """识别未说明排尿还是排便的口语化“上厕所”问题。"""
    normalized = re.sub(r"\s+", "", (query or "").lower())
    if not any(term in normalized for term in _AMBIGUOUS_ELIMINATION_TERMS):
        return False
    explicit = _EXPLICIT_URINARY_TERMS + _EXPLICIT_STOOL_TERMS
    return not any(term in normalized for term in explicit)


def is_cat_chin_specific_query(query: str, species: str | None = None) -> bool:
    """高精度识别猫下巴皮损，避免误注入腰背/尾根跳蚤皮炎卡片。"""
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
    """高精度日程问题优先使用信息完整的专属卡，避免泛化驱虫卡排在前面。"""
    normalized = re.sub(r"\s+", "", (query or "").lower())
    cat_context = normalize_species(species) == "cat" or "猫" in normalized
    kitten_context = any(term in normalized for term in ("幼猫", "小猫", "奶猫"))
    schedule_context = any(term in normalized for term in ("驱虫", "除虫", "疫苗", "免疫"))
    if cat_context and kitten_context and schedule_context:
        return "V17-CAT-PED-001"
    dog_context = normalize_species(species) == "dog" or any(
        term in normalized for term in ("狗", "犬")
    )
    if dog_context and any(term in normalized for term in _EXPLICIT_URINARY_TERMS):
        return "MVP-UR-001"
    return None


def promote_preferred_card(
    ranked: list[tuple[float, float, dict]], query: str, species: str | None = None
) -> list[tuple[float, float, dict]]:
    card_id = preferred_card_id(query, species)
    if not card_id:
        return ranked
    preferred = [item for item in ranked if item[2].get("id") == card_id]
    if not preferred:
        return ranked
    return preferred + [item for item in ranked if item[2].get("id") != card_id]


def select_supported_facts(card: dict, query: str, *, limit: int = 4) -> list[str]:
    """优先投影与本次问题最相关的事实，避免固定前3条漏掉驱虫等后半部分。"""
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
    """
    if result.decision is not RagDecisionStatus.SUFFICIENT:
        return False
    if result.top_score is None or result.top_score < fast_threshold:
        return False
    if not result.hits:
        return False
    cards_by_id = {card["id"]: card for card in report.cards}
    top_card = cards_by_id.get(result.hits[0].card_id)
    if not top_card:
        return False
    scope = str(top_card.get("scope", ""))
    # 精确匹配：只放行"纯简单问答/健康宣教"卡片；
    # 不放行 common_disease_health_education_and_triage（分诊类需走追问，2026-08-19 修复）
    return scope == "simple_owner_question" or scope == "common_disease_health_education"


_SPECIES_WORD_MAP: dict[str, list[tuple[str, str]]] = {
    # query 物种 -> [(卡片措辞, 替换为), ...]（先长词后短词）
    "cat": [("狗狗", "猫咪"), ("犬", "猫"), ("狗", "猫")],
    "dog": [("猫咪", "狗狗"), ("猫", "犬")],
}


def _adapt_species_text(text: str, query_species: str | None) -> str:
    """直答渲染时按提问物种替换卡片措辞（v1.5：修"问猫答犬"类问题）。"""
    if not query_species or not text:
        return text
    for src, dst in _SPECIES_WORD_MAP.get(query_species, []):
        text = text.replace(src, dst)
    return text


def build_fast_answer(report: RagAssetReport, result: RagResult, query_species: str | None = None) -> dict:
    """把命中卡片渲染成直答 payload（不调生成模型）。

    返回 dict 供 agent 组装 GeneratedConsultation：summary 用卡片标题与
    事实，what_to_do_now 用 safe_next_step，追问用 questions_to_ask。
    """
    cards_by_id = {card["id"]: card for card in report.cards}
    card = cards_by_id.get(result.hits[0].card_id, {})
    facts = [str(item)[:120] for item in card.get("source_supported_simple_facts", [])][:2]
    title = str(card.get("title", ""))[:80]
    if query_species:
        title = _adapt_species_text(title, query_species)
        facts = [_adapt_species_text(f, query_species) for f in facts]
    summary_parts = [title]
    summary_parts.extend(facts)
    safe_next_step = _adapt_species_text(
        str(card.get("safe_next_step", ""))[:240], query_species
    )
    questions = [
        _adapt_species_text(str(q)[:100], query_species)
        for q in card.get("questions_to_ask", [])
    ][:2]
    return {
        "card_id": card.get("id", ""),
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
    """
    if not species:
        return None
    lowered = species.strip().lower()
    if lowered in {"cat", "feline", "猫", "猫咪", "幼猫", "老年猫"} or "猫" in lowered:
        return "cat"
    if lowered in {"dog", "canine", "犬", "狗", "狗狗", "幼犬", "老年犬"} or any(
        marker in lowered for marker in ("犬", "狗")
    ):
        return "dog"
    return None