"""混合检索: 词面(0.7) + BGE-M3 向量(0.3), 判定阈值 0.32。

A/B 实测结论(2026-08-15, AutoDL RTX 5090):
  权重 0.7/0.3 + 阈值 0.32 在"拒绝探测 4/4 全过"约束下的最优参数:
  词面提升 124/127 (98%), 混合 recall@1 0.969 / recall@4 0.981 (词面 0.966/0.975)。
  混合检索可缓解的失败: 完全改写表达(如"我家猫感冒吗")与卡片相似度过低, 无法拒绝。

embedding 模型不可用(路径未配置 / FlagEmbedding 未安装 / 加载失败)时
自动回退纯词面模式(alpha=1, 阈值 0.24), 与 ShadowRetriever 完全一致。

2026-08-18 变更: 模型加载与卡片向量构建改为后台线程执行(不阻塞启动);
卡片向量持久化到 runtime/bge_card_emb.npy, 重启免重算; 索引就绪前检索自动回退词面。

【与 ShadowRetriever 的关系（改动前必读）】
本类是 ShadowRetriever 的“加一层向量”版本，二者共用同一套：
  - 查询门禁（含糊/歧义/猫下巴）；
  - 词面子分与权重（_overlap / _phrase_score / _query_terms / _terms）；
  - 输出契约（RagResult / RagHit）与证据投影、直答通道。
因此修词面评分只能改 retriever.py（本文件直接 import 其私有工具函数）。
本模块只新增两件事：① 向量精排；② 索引未就绪时的透明降级。

【降级的三个层次（设计核心）】
  1) 模型未配置（model_path 为空）→ client=None，永久纯词面，零额外开销；
  2) 模型加载中/失败 → lexical_only 为 True，阈值回到 0.24 操作点；
  3) 卡片向量索引未就绪 → dense=None，final_score 退化为纯词面分。
三层均在请求线程上同步判定，任何一层都不会让请求等待或失败。
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

# BGE 系列要求的检索指令前缀：BGE-M3 训练时区分“查询”与“文档”两种输入形态，
# 查询侧必须带该前缀，否则查询与文档向量不在同一空间，点积分不可比。
# 注意：只在查询侧加，卡片侧（_card_text）不加。
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# 卡片向量持久化路径：runtime/bge_card_emb.npy（parents[2] = 仓库根目录）
# 放到 runtime/ 下而不入版本库：它是可重建的派生数据，随卡片资产变化而失效。
_EMBED_CACHE_PATH = Path(__file__).resolve().parents[2] / "runtime" / "bge_card_emb.npy"


class EmbeddingClient:
    """BGE-M3 嵌入客户端: 后台线程加载, 未就绪不阻塞调用方。

    【状态机（三态，单向前进）】
        loading  → 后台线程正在加载，available=False
        ready    → _model 已就绪，available=True
        failed   → _error 已写入，available=False 且**不再重试**

    “failed 是终态”是刻意设计：模型路径错误/依赖缺失都属于配置类问题，
    重试只会反复耗时；一旦失败就永久回退纯词面，把故障暴露在日志里。
    """

    def __init__(self, model_path: str):
        self.model_path = model_path
        # 以下 5 个字段全部受 _lock 保护（除 _thread 仅在赋值/读取引用时用到）
        self._model: Any | None = None
        self._error: str | None = None   # 非 None 即表示“加载终态失败”，不重试
        self._lock = threading.Lock()
        self._loading = False            # 正在加载中（防重复启动线程）
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        """模型是否可用（加锁读取，避免读到写入中的不一致状态）。"""
        with self._lock:
            return self._error is None and self._model is not None

    def start_load_async(self) -> None:
        """后台线程加载模型; 重复调用无副作用。

        三重短路条件必须齐全，否则会重复起线程：
        _model 已就绪 / 已失败 (_error) / 正在加载 (_loading)。
        判空与置 _loading=True 在同一把锁内完成，避免两个请求同时起线程。
        """
        with self._lock:
            if self._model is not None or self._error is not None or self._loading:
                return
            self._loading = True
        # daemon=True：进程退出时不阻塞（模型加载可能耗时数十秒）
        self._thread = threading.Thread(target=self._load_blocking, daemon=True)
        self._thread.start()

    def _load_blocking(self) -> None:
        """后台线程实际执行的模型加载逻辑。

        加载成功写入 self._model；任何异常写入 self._error 并记录警告，
        调用方据此回退纯词面模式。fp16 仅在 GPU 可用时启用
        （CPU 上 fp16 推理会卡死，2026-08-18 修复）。

        注意：本方法只应在后台线程执行；请求线程调用 _load(blocking=True)
        时会 join 本线程而不是直接调用（见 _load）。
        """
        model: Any | None = None
        try:
            # 延迟导入：未启用向量检索的环境无需安装 FlagEmbedding / torch
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
            # 宽泛捕获：加载失败原因可能是 import/显存/文件损坏等，
            # 对本模块而言处理方式相同——记下原因并永久回退词面
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            logger.warning("BGE-M3 加载失败, 混合检索回退纯词面: %s", self._error)
        finally:
            # 无论成功失败都要复位 _loading，否则 start_load_async 永久短路
            with self._lock:
                self._loading = False

    def _load(self, blocking: bool = True) -> bool:
        """确保模型就绪。blocking=False 时未就绪立即返回 False(不等待)。

        【为什么必须判断 thread is not current_thread】
        后台构建线程内部会以 blocking=True 调用本方法；若不做自连接判断，
        就会 “join 自己” 造成永久死锁。

        :param blocking: True 等待加载完成（仅后台线程用）；
                         False 仅做就绪性探测（请求线程用）
        :return: True 表示模型可用
        """
        # 快路径：已就绪 / 已失败都不需要碰锁外的任何逻辑
        with self._lock:
            if self._model is not None:
                return True
            if self._error is not None:
                return False
        if not blocking:
            # 请求线程路径：不启动加载、不等待，未就绪就交给上层回退词面
            return False
        self.start_load_async()
        thread = self._thread
        # 只有“当前不在加载线程内”才 join，避免自连接死锁
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        return self.available

    def encode(self, texts: list[str], blocking: bool = False) -> Any | None:
        """返回 (n, 1024) L2 归一化向量; 模型未就绪或失败返回 None。

        【为什么要在客户端做 L2 归一化】
        上层用矩阵点积当作余弦相似度（normalized dot product）。
        归一化放在这里做一次，比每次检索时再算便宜得多；
        同时缓存到磁盘的也是归一化后向量，保证缓存与实时计算同口径。

        异常同样写入 _error：编码阶段失败（如显存 OOM）与加载失败同性质，
        继续使用只会持续报错，因此一并转为“终态失败 + 回退词面”。

        :param blocking: 透传给 _load；请求线程应保持 False
        """
        if not self._load(blocking=blocking):
            return None
        model = self._model
        if model is None:
            # 防御：_load 返回 True 但模型被并发清空（当前无此路径）
            return None
        try:
            import numpy as np

            vecs = np.asarray(
                model.encode(texts, return_dense=True)["dense_vecs"],
                dtype="float32",
            )
            # 逐行 L2 归一化，保证点积 == 余弦相似度
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            # 零向量兜底：置 1 避免除零产生 NaN（NaN 会污染后续所有排序）
            norms[norms == 0] = 1.0
            return vecs / norms
        except Exception as exc:  # pragma: no cover - 环境相关
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            logger.warning("BGE-M3 编码失败, 混合检索回退纯词面: %s", self._error)
            return None


class HybridRetriever:
    """词面 + 向量混合检索。接口与 ShadowRetriever 对齐, 输出同款 RagResult。

    【两阶段检索的原因】
    全量卡片跑向量编码代价高（即便卡片只有上百张，查询侧仍需一次前向）。
    因此先用零成本的词面分粗筛出 coarse_top_k 张，只对候选算向量分。
    两者相加的两阶段合计分与“全量混合”接近，但向量计算量降低一个数量级。

    【为何不去掉词面分只留向量】
    词面分承担两个不可替代的职责：① 粗筛依据；② 向量不可用时的退化回退。
    且实测混合仅带来小幅提升（recall@4 0.975 → 0.981），
    保留 0.7 的绝对值给词面是把“不可解释/不可控”的权重压到最低。
    """

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
        # 以下字段均为只读配置，实例创建后不再变更（线程安全前提）
        self.report = report
        self.top_k = top_k
        self.threshold = threshold
        self.alpha = alpha
        self.fast_threshold = fast_threshold
        self.coarse_top_k = coarse_top_k
        # 模型路径为空时不创建 EmbeddingClient，retriever 永久处于纯词面模式
        self.client = EmbeddingClient(model_path) if model_path else None
        # 卡片向量矩阵（懒加载）；_card_emb_lock 保护“双重检查 + 构建”过程
        self._card_emb: Any | None = None
        self._card_emb_lock = threading.Lock()

    @property
    def lexical_only(self) -> bool:
        """当前是否处于纯词面模式（向量模型未配置/未就绪/加载失败）。

        非阻塞检查：模型未就绪时按词面模式处理，后台就绪后自动切换混合模式。

        注意这是“即时快照”而非缓存值：同一进程在不同时刻可能返回不同结果，
        因此调用方不要跨阶段缓存它，否则会出现“阈值按词面取、评分按混合算”的错配。
        """
        if self.client is None:
            return True
        # blocking=False：只用就绪性探测，请求线程绝不触发模型加载
        return not self.client._load(blocking=False)

    def effective_threshold(self) -> float:
        """当前生效的判定阈值：纯词面回退时沿用 ShadowRetriever 的 0.24 操作点，
        混合模式使用 0.32。

        为什么必须分两个阈值：词面分与混合分尺度不同。
        混合模式加入了向量分（可能拉高也可能拉低），0.32 是该尺度下的实测最优；
        若回退词面后仍用 0.32，就会比 ShadowRetriever 严格，
        造成“模型挂了反而更难命中知识库”的反直觉行为。
        """
        return 0.24 if self.lexical_only else self.threshold

    def _card_text(self, card: dict) -> str:
        """构建卡片的向量化文本：标题 + 检索文本 + 前 3 条事实拼接。

        只取前 3 条事实是成本与效果的折中：
        - 全量事实会显著拉长序列长度，编码耗时上升；
        - 前 3 条通常是该卡最核心的表述（资产编辑顺序即重要性顺序）。
        若后续发现召回不足，优先调整此处而非直接换模型。
        """
        facts = " ".join(str(f) for f in card.get("source_supported_simple_facts", [])[:3])
        return " ".join([str(card.get("title", "")), str(card.get("retrieval_text", "")), facts])

    def _card_embeddings(self, blocking: bool = False) -> Any | None:
        """卡片向量(含持久化缓存)。仅后台线程(blocking=True)构建索引;
        非阻塞调用方在索引未就绪时直接返回 None(回退词面), 绝不触发全量编码。

        【并发设计（三段）】
        ① 请求线程(blocking=False)：索引未就绪立即返回 None，不等待；
        ② 后台线程(blocking=True)：双重检查锁内构建，避免重复编码；
        ③ 磁盘缓存：命中且行数一致则直接复用，重启免重算。

        【为什么缓存要校验 arr.shape[0] == len(texts)】
        卡片资产会增删。若卡片数变化而缓存未失效，索引与卡片列表会错位，
        导致 dense[index] 对应到错误的卡片（静默给出错排序）。
        因此行数不一致就当作未命中，重新编码并覆盖缓存。
        """
        import numpy as np  # noqa: PLC0415 - 延迟导入, 词面模式下零依赖

        if self.client is None:
            return None
        # blocking=True 等待模型就绪(后台线程用); blocking=False 未就绪即回退词面
        if not self.client._load(blocking=blocking):
            return None
        if self._card_emb is None and not blocking:
            return None  # 索引由后台线程构建; 请求线程不等待
        if self._card_emb is None:
            # 双重检查锁：先在锁外判空（快路径命中），再在锁内二次判空
            # （防止两个后台调用同时进入构建）
            with self._card_emb_lock:
                if self._card_emb is None:
                    texts = [self._card_text(c) for c in self.report.cards]
                    # 尝试读持久化缓存
                    cache = _EMBED_CACHE_PATH
                    if cache.exists():
                        try:
                            arr = np.load(cache)
                            # 行数一致才可用：见 docstring “为什么缓存要校验行数”
                            if arr.shape[0] == len(texts):
                                self._card_emb = arr
                                logger.info("BGE 卡片向量缓存命中: %s", cache)
                                return self._card_emb
                        except Exception:  # pragma: no cover - 缓存损坏时重算
                            # 缓存损坏/格式变化：静默忽略，走下面的重新编码
                            pass
                    vecs = self.client.encode(texts, blocking=blocking)
                    if vecs is not None:
                        self._card_emb = vecs
                        try:
                            # 写缓存失败不致命：只是下次重启需重算
                            cache.parent.mkdir(parents=True, exist_ok=True)
                            np.save(cache, vecs)
                            logger.info("BGE 卡片向量已缓存: %s", cache)
                        except Exception as exc:  # pragma: no cover - 缓存失败不影响功能
                            logger.warning("BGE 卡片向量缓存写入失败: %s (%s)", cache, exc)
        return self._card_emb

    def warmup(self) -> bool:
        """启动预热(非阻塞): 后台线程加载模型并构建卡片向量索引(含持久化缓存)。

        立即返回 False; 模型就绪前检索自动回退纯词面, 就绪后自动启用混合模式。

        【为什么返回值恒为 False】
        本方法语义是“已发起预热”而非“预热完成”。旧版曾阻塞启动直到索引就绪，
        导致服务启动时间被模型加载主导；现改为完全后台化。
        调用方不应依赖返回值做判断（该返回值仅保留兼容性）。
        """
        if self.client is None:
            return False
        self.client.start_load_async()

        def _build() -> None:
            try:
                # 先等待模型加载完成(join 加载线程), 再构建卡片索引。
                # 此调用发生在 _build 线程内，因此 _load 不会 join 自己（已做判断）。
                if not self.client._load(blocking=True):
                    logger.info("BGE 模型不可用, 索引跳过(回退词面)")
                    return
                ok = self._card_embeddings(blocking=True) is not None
                logger.info(
                    "BGE 卡片索引就绪: %s", "hybrid" if ok else "fallback-lexical"
                )
            except Exception:  # pragma: no cover - 构建失败回退词面
                # 预热失败绝不能让进程崩：影响面仅是退化为词面检索
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
            # 与 ShadowRetriever 一致：资产不可用时不阻断请求，返回 UNAVAILABLE
            return RagResult(
                query=query,
                decision=RagDecisionStatus.UNAVAILABLE,
                reason_codes=list(self.report.errors) or ["asset_unavailable"],
                index_version=self.report.index_version,
                retrieval_ms=(time.perf_counter() - started) * 1000,
            )

        # 以下三道查询门禁与 ShadowRetriever 完全共用（同一批函数），
        # 保证“换检索器不改变安全边界”：命中任一即早退，hits=[]。
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

        # 归一化物种（用于过滤与阈值放宽判定）
        normalized_species = normalize_species(species)
        query_terms = _query_terms(query)

        # ---- 向量侧准备（失败/未就绪必须保持 dense=None，不得影响主流程）----
        # dense 是 (卡片数,) 的点积分数组：dense[i] 对应 self.report.cards[i]，
        # 因此阶段 2 会用“原始卡片下标 index”去取值（不是候选列表下标）。
        dense: Any | None = None
        if not self.lexical_only:
            try:
                import numpy as np

                # 两张向量都用 blocking=False：请求线程绝不等待索引/编码完成
                card_emb = self._card_embeddings(blocking=False)
                # 查询侧必须带检索指令前缀（见 QUERY_INSTRUCTION 注释）
                q_vec = self.client.encode([QUERY_INSTRUCTION + query], blocking=False)
                if card_emb is not None and q_vec is not None:
                    # 矩阵×向量 → 每个卡片一个分；向量已 L2 归一化，故点积即余弦
                    dense = np.asarray(card_emb) @ np.asarray(q_vec)[0]
            except Exception:  # pragma: no cover - 环境相关
                # 任何异常都视为“向量不可用”：本次检索退化为纯词面，不向上抛
                dense = None

        # 阶段 1: 词面粗筛(全量卡片只算轻量词面分)
        # 四元组：(lexical, keyword_score, card, index) —— index 是关键，
        # 阶段 2 靠它从 dense 里取对应卡片的向量分。
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
            # （粗筛闸门必须比精排更严：一旦放进来，向量分就有可能把它抬到前排）
            if keyword_score <= 0 and phrase_score <= 0 and category_score <= 0:
                continue
            # 词面分公式与权重必须与 ShadowRetriever 完全一致，
            # 否则“回退词面”与“混合模式”的分数不再可比，阈值也无法共用 0.24。
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
        # 同样用 ID 作为第二排序键，保证同分顺序可复现
        coarse.sort(key=lambda item: (-item[0], item[2].get("id", "")))
        # 只留前 coarse_top_k（默认 30）进入向量精排
        candidates = coarse[: self.coarse_top_k]

        # 阶段 2: dense 精排(仅候选, 避免无关卡片参与混合分)
        # 注意 dense[index] 里用的是“卡片在 report.cards 中的原始下标”，
        # 不是 candidates 里的位置——改错会导致分数张冠李戴（无异常、结果全错）。
        ranked: list[tuple[float, float, dict]] = []
        for lexical, keyword_score, card, index in candidates:
            if dense is not None:
                # 加权融合：alpha 越大越信任词面；两者均为 0~1，故结果也落 0~1
                final_score = self.alpha * lexical + (1 - self.alpha) * float(dense[index])
            else:
                # 退化路径：dense 缺失（模型未就绪/编码失败）时 final = 词面分，
                # 此时等价于 ShadowRetriever（但阈值仍需用 effective_threshold 复查）
                final_score = lexical
            if final_score > 0:
                ranked.append((final_score, keyword_score, card))
        ranked.sort(key=lambda item: (-item[0], item[2].get("id", "")))
        # 倾向性提升与 ShadowRetriever 共用同一实现（保持两路行为一致）
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
                # 语义分字段恒为 0.0：与 ShadowRetriever 保持契约一致，
                # dense 分只参与 final_score 融合。若需观测请新增字段，不要改本字段语义。
                semantic_score=0.0,
                keyword_score=keyword_score,
                final_score=final_score,
            )
            for final_score, keyword_score, card in selected
        ]
        top_score = max((hit.final_score for hit in hits), default=None)
        threshold = self.effective_threshold()
        if normalized_species is None:
            # 无物种信息时词面部分少 0.08 物种加分(混合后少 0.08*alpha), 等额放宽判定阈值。
            # 注意这里只放宽有向量时的融合损失；纯词面回退时 alpha 仍为原值，
            # 该式也同样成立（词面分权重为 1）。
            threshold -= 0.08 * self.alpha
        reasons: list[str] = []
        if not hits or (top_score is not None and top_score < threshold):
            decision = RagDecisionStatus.INSUFFICIENT
            reasons.append("low_relevance")
        else:
            decision = RagDecisionStatus.SUFFICIENT
        # 政策限制词一票否决：与 ShadowRetriever 同一位置、同一优先级
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
        最多 2 张卡片、每张 4 条事实，防止资产元数据泄漏进 prompt。

        两份实现故意保持逐字一致（含各字段截断长度）：
        它们是同一个“契约”的两份副本，修改时必须同步，
        否则切换检索器会导致注入 prompt 的证据字段发生变化。
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
        """实例方法包装：判断是否满足卡片直答条件（高置信简单问答/健康宣教卡）。

        用实例的 self.fast_threshold（默认 0.55），比模块级默认值 0.4 更严。
        与混合/纯词面模式无关：直答门槛只看分数与卡片 scope，不看向量。
        """
        return is_fast_answerable(self.report, result, fast_threshold=self.fast_threshold)

    def build_fast_answer(self, result: RagResult, query_species: str | None = None) -> dict:
        """实例方法包装：把命中卡片渲染为直答 payload（不调生成模型），
        并按提问物种替换卡片中的猫/犬措辞。"""
        return build_fast_answer(self.report, result, query_species=query_species)
