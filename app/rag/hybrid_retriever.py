"""混合检索: 词面(0.7) + BGE-M3 向量(0.3), 判定阈值 0.32。

A/B 实测结论(2026-08-15, AutoDL RTX 5090):
  权重 0.7/0.3 + 阈值 0.32 在"拒绝探测 4/4 全过"约束下的最优参数:
  词面提升 124/127 (98%), 混合 recall@1 0.969 / recall@4 0.981 (词面 0.966/0.975)。
  混合检索可缓解的失败: 完全改写表达(如"我家猫感冒吗")与卡片相似度过低, 无法拒绝。

embedding 模型不可用(路径未配置 / FlagEmbedding 未安装 / 加载失败)时
自动回退纯词面模式(alpha=1, 阈值 0.24), 与 ShadowRetriever 完全一致。

2026-08-18 变更: 模型加载与卡片向量构建改为后台线程执行(不阻塞启动);
卡片向量持久化到 runtime/bge_card_emb.npy, 重启免重算; 索引就绪前检索自动回退词面。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from app.rag.loader import RagAssetReport
from app.rag.models import RagDecisionStatus, RagHit, RagResult
from app.rag.retriever import (
    _overlap,
    _phrase_score,
    _query_terms,
    _terms,
    _POLICY_TERMS,
    build_fast_answer,
    is_ambiguous_elimination_query,
    is_cat_chin_specific_query,
    is_vague_general_query,
    is_fast_answerable,
    normalize_species,
    promote_preferred_card,
    select_supported_facts,
)

logger = logging.getLogger(__name__)

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

_EMBED_CACHE_PATH = Path(__file__).resolve().parents[2] / "runtime" / "bge_card_emb.npy"


class EmbeddingClient:
    """BGE-M3 嵌入客户端: 后台线程加载, 未就绪不阻塞调用方。"""

    def __init__(self, model_path: str):
        self.model_path = model_path
        self._model: Any | None = None
        self._error: str | None = None
        self._lock = threading.Lock()
        self._loading = False
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        with self._lock:
            return self._error is None and self._model is not None

    def start_load_async(self) -> None:
        """后台线程加载模型; 重复调用无副作用。"""
        with self._lock:
            if self._model is not None or self._error is not None or self._loading:
                return
            self._loading = True
        self._thread = threading.Thread(target=self._load_blocking, daemon=True)
        self._thread.start()

    def _load_blocking(self) -> None:
        """后台线程实际执行的模型加载逻辑。

        加载成功写入 self._model；任何异常写入 self._error 并记录警告，
        调用方据此回退纯词面模式。fp16 仅在 GPU 可用时启用
        （CPU 上 fp16 推理会卡死，2026-08-18 修复）。
        """
        model: Any | None = None
        try:
            from FlagEmbedding import BGEM3FlagModel
            import torch

            # fp16 仅在 GPU 上启用: CPU 上 fp16 推理会卡死(2026-08-18 修复)
            use_fp16 = torch.cuda.is_available()
            device = "cuda" if use_fp16 else "cpu"
            model = BGEM3FlagModel(self.model_path, use_fp16=use_fp16, device=device)
            with self._lock:
                self._model = model
            logger.info("BGE-M3 嵌入模型已加载（path=%s）", self.model_path)
        except Exception as exc:  # pragma: no cover - 环境相关
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            logger.warning("BGE-M3 加载失败, 混合检索回退纯词面: %s", self._error)
        finally:
            with self._lock:
                self._loading = False

    def _load(self, blocking: bool = True) -> bool:
        """确保模型就绪。blocking=False 时未就绪立即返回 False(不等待)。"""
        with self._lock:
            if self._model is not None:
                return True
            if self._error is not None:
                return False
        if not blocking:
            return False
        self.start_load_async()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        return self.available

    def encode(self, texts: list[str], blocking: bool = False) -> Any | None:
        """返回 (n, 1024) L2 归一化向量; 模型未就绪或失败返回 None。"""
        if not self._load(blocking=blocking):
            return None
        model = self._model
        if model is None:
            return None
        try:
            import numpy as np

            vecs = np.asarray(
                model.encode(texts, return_dense=True)["dense_vecs"],
                dtype="float32",
            )
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            return vecs / norms
        except Exception as exc:  # pragma: no cover - 环境相关
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            logger.warning("BGE-M3 编码失败, 混合检索回退纯词面: %s", self._error)
            return None


class HybridRetriever:
    """词面 + 向量混合检索。接口与 ShadowRetriever 对齐, 输出同款 RagResult。"""

    def __init__(
        self,
        report: RagAssetReport,
        *,
        top_k: int = 4,
        threshold: float = 0.32,
        alpha: float = 0.7,
        model_path: str = "",
        fast_threshold: float = 0.55,
        coarse_top_k: int = 30,
    ):
        """初始化混合检索器。

        :param report: 知识资产报告（RagAssetLoader.load() 产出）
        :param top_k: 最终返回的卡片数
        :param threshold: 混合模式判定阈值（A/B 实测最优 0.32）
        :param alpha: 词面分权重；向量分权重为 1-alpha（0.7/0.3）
        :param model_path: BGE-M3 模型本地路径；为空则不启用向量检索
        :param fast_threshold: 卡片直答置信度阈值
        :param coarse_top_k: 词面粗筛后送入向量精排的候选数
        """
        self.report = report
        self.top_k = top_k
        self.threshold = threshold
        self.alpha = alpha
        self.fast_threshold = fast_threshold
        self.coarse_top_k = coarse_top_k
        # 模型路径为空时不创建 EmbeddingClient，retriever 永久处于纯词面模式
        self.client = EmbeddingClient(model_path) if model_path else None
        self._card_emb: Any | None = None
        self._card_emb_lock = threading.Lock()

    @property
    def lexical_only(self) -> bool:
        """当前是否处于纯词面模式（向量模型未配置/未就绪/加载失败）。

        非阻塞检查：模型未就绪时按词面模式处理，后台就绪后自动切换混合模式。
        """
        if self.client is None:
            return True
        return not self.client._load(blocking=False)

    def effective_threshold(self) -> float:
        """当前生效的判定阈值：纯词面回退时沿用 ShadowRetriever 的 0.24 操作点，
        混合模式使用 0.32。"""
        return 0.24 if self.lexical_only else self.threshold

    def _card_text(self, card: dict) -> str:
        """构建卡片的向量化文本：标题 + 检索文本 + 前 3 条事实拼接。"""
        facts = " ".join(str(f) for f in card.get("source_supported_simple_facts", [])[:3])
        return " ".join([str(card.get("title", "")), str(card.get("retrieval_text", "")), facts])

    def _card_embeddings(self, blocking: bool = False) -> Any | None:
        """卡片向量(含持久化缓存)。仅后台线程(blocking=True)构建索引;
        非阻塞调用方在索引未就绪时直接返回 None(回退词面), 绝不触发全量编码。"""
        import numpy as np  # noqa: PLC0415 - 延迟导入, 词面模式下零依赖

        if self.client is None:
            return None
        # blocking=True 等待模型就绪(后台线程用); blocking=False 未就绪即回退词面
        if not self.client._load(blocking=blocking):
            return None
        if self._card_emb is None and not blocking:
            return None  # 索引由后台线程构建; 请求线程不等待
        if self._card_emb is None:
            with self._card_emb_lock:
                if self._card_emb is None:
                    texts = [self._card_text(c) for c in self.report.cards]
                    # 尝试读持久化缓存
                    cache = _EMBED_CACHE_PATH
                    if cache.exists():
                        try:
                            arr = np.load(cache)
                            if arr.shape[0] == len(texts):
                                self._card_emb = arr
                                logger.info("BGE 卡片向量缓存命中: %s", cache)
                                return self._card_emb
                        except Exception:  # pragma: no cover - 缓存损坏时重算
                            pass
                    vecs = self.client.encode(texts, blocking=blocking)
                    if vecs is not None:
                        self._card_emb = vecs
                        try:
                            cache.parent.mkdir(parents=True, exist_ok=True)
                            np.save(cache, vecs)
                            logger.info("BGE 卡片向量已缓存: %s", cache)
                        except Exception as exc:  # pragma: no cover - 缓存失败不影响功能
                            logger.warning("BGE 卡片向量缓存写入失败: %s (%s)", cache, exc)
        return self._card_emb

    def warmup(self) -> bool:
        """启动预热(非阻塞): 后台线程加载模型并构建卡片向量索引(含持久化缓存)。

        立即返回 False; 模型就绪前检索自动回退纯词面, 就绪后自动启用混合模式。
        """
        if self.client is None:
            return False
        self.client.start_load_async()

        def _build() -> None:
            try:
                # 先等待模型加载完成(join 加载线程), 再构建卡片索引
                if not self.client._load(blocking=True):
                    logger.info("BGE 模型不可用, 索引跳过(回退词面)")
                    return
                ok = self._card_embeddings(blocking=True) is not None
                logger.info(
                    "BGE 卡片索引就绪: %s", "hybrid" if ok else "fallback-lexical"
                )
            except Exception:  # pragma: no cover - 构建失败回退词面
                logger.warning("BGE 索引构建异常, 回退纯词面模式", exc_info=True)

        threading.Thread(target=_build, daemon=True).start()
        return False

    def search(
        self, query: str, *, species: str | None = None, category: str | None = None
    ) -> RagResult:
        """执行混合检索（两阶段：词面粗筛 → dense 向量精排）。

        【处理流程】
        1. 资产不可用 → UNAVAILABLE；
        2. 含糊查询/歧义排泄/猫下巴专属查询 → INSUFFICIENT（与 ShadowRetriever
           共用同一套查询门禁，保证两种检索器行为一致）；
        3. 向量就绪时：非阻塞编码查询（带 BGE 检索指令前缀），与卡片向量
           矩阵做点积得到 dense 分；向量未就绪则 dense=None；
        4. 阶段 1 词面粗筛：全量卡片只算轻量词面分，取 coarse_top_k（30）候选；
        5. 阶段 2 精排：final = alpha*词面 + (1-alpha)*dense；dense 缺失时
           final = 词面分（自动退化为纯词面）；
        6. 倾向性卡片提升（promote_preferred_card）后取 top_k；
        7. 阈值判定（无物种信息时等额放宽 0.08*alpha）→ SUFFICIENT/INSUFFICIENT；
        8. 政策限制词 → POLICY_RESTRICTED。

        :param query: 用户查询文本
        :param species: 物种限定（cat/dog/None；未知物种只匹配猫狗通用卡）
        :param category: 分类限定（命中加 0.04）
        :return: RagResult（结构与 ShadowRetriever 完全一致）
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

        dense: Any | None = None
        if not self.lexical_only:
            try:
                import numpy as np

                card_emb = self._card_embeddings(blocking=False)
                q_vec = self.client.encode([QUERY_INSTRUCTION + query], blocking=False)
                if card_emb is not None and q_vec is not None:
                    dense = np.asarray(card_emb) @ np.asarray(q_vec)[0]
            except Exception:  # pragma: no cover - 环境相关
                dense = None

        # 阶段 1: 词面粗筛(全量卡片只算轻量词面分)
        coarse: list[tuple[float, float, dict, int]] = []
        for index, card in enumerate(self.report.cards):
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
            keyword_score = _overlap(query_terms, _terms(str(card.get("retrieval_text", ""))))
            title_score = _overlap(query_terms, _terms(str(card.get("title", ""))))
            phrase_score = _phrase_score(query, card.get("user_phrases", []))
            species_score = 0.08 if normalized_species else 0.0
            category_score = 0.04 if category and category == card.get("category") else 0.0
            # 不允许仅凭物种分进入 dense 精排，避免跨病种卡片污染回答与追问。
            if keyword_score <= 0 and phrase_score <= 0 and category_score <= 0:
                continue
            lexical = min(
                1.0,
                keyword_score * 0.72
                + title_score * 0.12
                + phrase_score * 0.2
                + species_score
                + category_score,
            )
            if lexical > 0:
                coarse.append((lexical, keyword_score, card, index))
        coarse.sort(key=lambda item: (-item[0], item[2].get("id", "")))
        candidates = coarse[: self.coarse_top_k]

        # 阶段 2: dense 精排(仅候选, 避免无关卡片参与混合分)
        ranked: list[tuple[float, float, dict]] = []
        for lexical, keyword_score, card, index in candidates:
            if dense is not None:
                final_score = self.alpha * lexical + (1 - self.alpha) * float(dense[index])
            else:
                final_score = lexical
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
        threshold = self.effective_threshold()
        if normalized_species is None:
            # 无物种信息时词面部分少 0.08 物种加分(混合后少 0.08*alpha), 等额放宽判定阈值
            threshold -= 0.08 * self.alpha
        reasons: list[str] = []
        if not hits or (top_score is not None and top_score < threshold):
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
        """与 ShadowRetriever 相同的证据投影：SUFFICIENT 时把命中卡片
        白名单字段（标题/物种/事实/安全建议/红旗）投影为模型上下文载荷，
        最多 2 张卡片、每张 4 条事实，防止资产元数据泄漏进 prompt。"""
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
        """实例方法包装：判断是否满足卡片直答条件（高置信简单问答/健康宣教卡）。"""
        return is_fast_answerable(self.report, result, fast_threshold=self.fast_threshold)

    def build_fast_answer(self, result: RagResult, query_species: str | None = None) -> dict:
        """实例方法包装：把命中卡片渲染为直答 payload（不调生成模型），
        并按提问物种替换卡片中的猫/犬措辞。"""
        return build_fast_answer(self.report, result, query_species=query_species)
