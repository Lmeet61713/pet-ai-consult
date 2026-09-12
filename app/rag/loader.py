"""RAG 知识资产的只读加载与校验（RagAssetLoader）。

【核心职责】
从 assets/rag/v1_x/ 目录加载知识卡片（JSONL）、来源清单（sources JSON）、
审核队列（veterinary_review_queue CSV）等资产，并在加载时执行多重完整性
校验，产出不可变的 RagAssetReport 供检索器使用。

【为什么需要这么多校验？】
知识卡片是医疗建议的事实来源，资产在正式发布前处于"测试层"状态。
加载时强制校验可以防止：
- 资产文件被篡改/损坏（SHA256 校验和、content_hash 内容哈希）；
- 未通过兽医审核的卡片误入线上（release gate：index_tier 必须为 test、
  production_eligible 必须为 False、veterinary_review.status 必须为 pending）；
- 卡片引用不存在的来源或缺少页码定位（证据可溯源）；
- 使用版权不允许商用的来源（v1.4~v1.7 强制 commercial_use_allowed）。

任一校验失败都会把错误码收集到 RagAssetReport.errors；errors 非空时
report.ready 为 False，检索器直接返回 UNAVAILABLE，绝不带病提供知识。

【校验失败的传播路径（可用性设计）】
    loader.errors
      → report.ready = False
      → 检索器返回 UNAVAILABLE，reason_codes = 错误码
      → ConsultAgent 标记 knowledge_consult 降级，不注入知识库增强，
         但**仍然继续完成问诊**（走无知识库的生成/追问路径）。
即：知识库是增强项而非必需项，不能成为单点故障。
因此本模块一律“收集错误、不抛异常”——抛异常会阻断服务启动。

【错误码命名规范（便于日志检索与聚合）】
- 文件/解析层：<对象>_file_missing / invalid_<对象>_json / invalid_<对象>_type
- 单条记录层：<问题>:<记录ID>[:<字段>]（冒号分隔，便于按卡片聚合错误）
- 前缀即性质：missing_ / invalid_ / duplicate_ / unresolved_ / mismatch /
  disallowed_ / non_test / production_eligible

【支持的资产格式】
- v1_1：早期 enriched JSONL 格式（facts + evidence_refs 结构）
- v1_4 ~ v1_8：combined 格式（source_supported_simple_facts + evidence），
  加载时通过 _normalize_v14_card() 归一化为与 v1_1 兼容的内部结构
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RagAssetReport:
    """知识资产加载报告（不可变）。

    :param cards: 校验通过并归一化后的知识卡片元组
    :param source_ids: 来源清单中全部合法来源 ID 的集合
    :param index_version: 索引版本号（来自 validation_report 或卡片字段）
    :param errors: 校验过程中收集的错误码元组；非空表示资产不可用
    :param asset_format: 资产格式（"v1_1"/"v1_4".../"unknown"）
    """

    cards: tuple[dict[str, Any], ...]
    source_ids: frozenset[str]
    index_version: str
    errors: tuple[str, ...] = ()
    asset_format: str = "unknown"

    @property
    def ready(self) -> bool:
        """资产是否可用于检索：必须有卡片且无任何校验错误。"""
        return bool(self.cards) and not self.errors


class RagAssetLoader:
    """知识卡片资产加载器：加载 v1.1 / v1.4~v1.8 测试层资产，不改变其发布状态。

    典型用法：
        report = RagAssetLoader("assets/rag/v1_8").load()
        if report.ready:
            retriever = ShadowRetriever(report)
    """

    # 各版本卡片 JSONL 文件名
    _V11_CARDS = "knowledge_cards.enriched.jsonl"
    _V14_CARDS = "knowledge_cards.combined_v1_4.jsonl"
    _V15_CARDS = "knowledge_cards.combined_v1_5.jsonl"
    _V16_CARDS = "knowledge_cards.combined_v1_6.jsonl"
    _V17_CARDS = "knowledge_cards.combined_v1_7.jsonl"
    _V18_CARDS = "knowledge_cards.combined_v1_8.jsonl"

    # v1.4 及以后格式共用同一套校验/归一化逻辑
    _V14_LIKE = ("v1_4", "v1_5", "v1_6", "v1_7", "v1_8")

    def __init__(self, asset_path: str | Path):
        """初始化加载器。

        :param asset_path: 资产目录路径，或直接指向某个卡片 JSONL 文件的路径；
                           传空值表示未配置（load() 返回 asset_path_missing 错误）
        """
        self.asset_path = Path(asset_path).expanduser() if asset_path else None

    def load(self) -> RagAssetReport:
        """加载并校验全部知识资产，返回 RagAssetReport。

        【流程】
        1. 解析路径，识别资产格式（v1_1 / v1_4~v1_8 / unknown）
        2. v1.4+ 格式：先校验 SHA256SUMS 整目录文件完整性
        3. 逐行读取卡片 JSONL（坏行记录错误并跳过）
        4. 加载来源清单（sources）并校验来源合法性/版权
        5. 读取兽医审核队列哈希（review queue）
        6. 逐卡片校验（必填字段、发布闸门、内容哈希、审核状态、证据溯源）
        7. v1.1 额外校验 facts.evidence_refs 的来源可解析
        8. 读取索引版本号

        任何步骤的错误都累积进 errors，不抛异常（保证服务可启动，
        检索侧通过 report.ready 决定是否可用）。

        【步骤编号与下方代码的对应关系】
        ① 空路径早退 → ② 识别格式/定位文件 → ③ 目录校验和（仅 v1.4+）
        → ④ 读卡片 → ⑤ 读来源清单 → ⑥ 读审核队列哈希
        → ⑦ 逐卡校验 + 归一化 → ⑧ v1.1 额外溯源校验 → ⑨ 读版本号。
        注意 ⑦ 的顺序：先校验（出错只记码）再归一化（无副作用），
        因此即使卡片不合法也会被归一化并放进 report.cards，
        调用方必须用 report.ready 而不是 cards 非空来判断资产可用。
        """
        # ① 未配置资产路径（如本地开发未设环境变量）：直接返回，不给卡片
        if self.asset_path is None:
            return RagAssetReport((), frozenset(), "unconfigured", ("asset_path_missing",))

        # ② 支持“目录”与“单个 JSONL 文件”两种入参，统一解析出根目录/卡片文件/格式
        root, cards_path, asset_format = self._resolve_paths(self.asset_path)
        if cards_path is None or not cards_path.is_file():
            # 没有可读的卡片文件就没有任何可检索内容 → 立即返回（不做后续无意义校验）
            return RagAssetReport(
                (), frozenset(), "unavailable", ("cards_file_missing",), asset_format
            )

        errors: list[str] = []
        if asset_format in self._V14_LIKE:
            # ③ v1.4+ 资产随包提供 SHA256SUMS：先验整个目录，
            #    这样“文件被篡改/半包上传”能在读卡片前就被发现
            self._validate_checksums(root, errors)

        # ④ 读卡片（坏行只记错误并跳过，不影响其余卡片）
        raw_cards = self._read_cards(cards_path, errors)
        # ⑤ 读来源清单：得到合法 source_id 集合，供后面 evidence 溯源校验
        source_ids = self._load_source_ids(root, asset_format, errors)
        # ⑥ 读兽医审核队列登记哈希（送审基线）；v1.1 无此机制故为空字典
        review_hashes = (
            read_review_queue(root, asset_format) if asset_format in self._V14_LIKE else {}
        )
        cards: list[dict[str, Any]] = []
        for line_number, raw_card in raw_cards:
            # ⑦ 两套格式走各自的校验器；校验与归一化分离，
            #    保证归一化函数（_normalize_*）永远是纯函数、无校验职责
            if asset_format in self._V14_LIKE:
                self._validate_v14_card(
                    raw_card, line_number, source_ids, review_hashes, errors
                )
                cards.append(_normalize_v14_card(raw_card))
            else:
                self._validate_v11_card(raw_card, line_number, errors)
                cards.append(raw_card)

        if asset_format == "v1_1":
            # ⑧ v1.1 独有：evidence_refs 的 source_id 必须在来源清单中可解析。
            #    （v1.4+ 在 _validate_v14_card 里逐卡同步做过了）
            for card in cards:
                for fact in card.get("facts", []):
                    for ref in fact.get("evidence_refs", []):
                        source_id = ref.get("source_id")
                        if source_id not in source_ids:
                            errors.append(f"unresolved_source:{card.get('id')}:{source_id}")

        # ⑨ 读版本号：写入 RagResult.index_version，线上定位资产版本的唯一依据
        version = self._read_index_version(root, cards, asset_format)
        return RagAssetReport(
            tuple(cards),
            frozenset(source_ids),
            version,
            # 保序去重：同一错误可能在多卡上重复出现，报告里只需保留一条
            tuple(dict.fromkeys(errors)),
            asset_format,
        )

    @classmethod
    def _resolve_paths(cls, path: Path) -> tuple[Path, Path | None, str]:
        """根据传入路径定位资产根目录、卡片文件路径与资产格式。

        支持两种入参：
        - 直接指向某个卡片 JSONL 文件 → 以其父目录为根，按文件名识别格式；
          无法识别则按 v1_1 处理
        - 指向目录 → 依次探测 v1_4~v1_8 的卡片文件名，再退回 v1.1，
          都不存在则返回 cards_path=None（格式 unknown）

        :return: (资产根目录, 卡片文件路径或 None, 格式标识)
        """
        # 情形 A：入参直接指向文件 —— 用文件名反推格式（无法识别时按 v1_1 处理，
        # 因为 v1_1 不依赖 SHA256SUMS/审核队列，兜底最安全）
        if path.is_file():
            for fmt, name in (("v1_4", cls._V14_CARDS), ("v1_5", cls._V15_CARDS),
                              ("v1_6", cls._V16_CARDS), ("v1_7", cls._V17_CARDS),
                              ("v1_8", cls._V18_CARDS)):
                if path.name == name:
                    return path.parent, path, fmt
            return path.parent, path, "v1_1"
        # 情形 B：入参为目录 —— 按 v1_4 → v1_8 的固定顺序探测，先命中先返回。
        # 注意顺序是从小到大：若同一目录里同时存在多个版本的卡片文件，
        # 选中的会是序号最小的那个（正常部署中每个版本各自独立目录，不会混放）。
        for fmt, name in (("v1_4", cls._V14_CARDS), ("v1_5", cls._V15_CARDS),
                          ("v1_6", cls._V16_CARDS), ("v1_7", cls._V17_CARDS),
                          ("v1_8", cls._V18_CARDS)):
            candidate = path / name
            if candidate.is_file():
                return path, candidate, fmt
        v11 = path / cls._V11_CARDS
        if v11.is_file():
            return path, v11, "v1_1"
        # 都不存在：返回 None 让 load() 报 cards_file_missing，
        # 而不是返回一个“看似可用的空资产”（后者会静默检索不到任何东西）
        return path, None, "unknown"

    @staticmethod
    def _read_cards(
        cards_path: Path, errors: list[str]
    ) -> list[tuple[int, dict[str, Any]]]:
        """逐行读取卡片 JSONL 文件。

        - 空行跳过；
        - JSON 解析失败记录 invalid_json_line:{行号} 并跳过；
        - 非对象（如数组/字符串）记录 invalid_card_type:{行号} 并跳过。

        :return: (行号, 卡片字典) 列表
        """
        cards: list[tuple[int, dict[str, Any]]] = []
        # 行号从 1 开始：与编辑器/日志里的行号一致，便于定位坏行
        for line_number, line in enumerate(cards_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                # 文件尾部的空行很常见，不算错误
                continue
            try:
                card = json.loads(line)
            except json.JSONDecodeError:
                # 单行损坏不放弃整个文件：记录行号后继续读下一行
                errors.append(f"invalid_json_line:{line_number}")
                continue
            if not isinstance(card, dict):
                # JSONL 里混入数组/字符串（多为合并文件的意外产物）
                errors.append(f"invalid_card_type:{line_number}")
                continue
            cards.append((line_number, card))
        return cards

    @staticmethod
    def _validate_v11_card(
        card: dict[str, Any], line_number: int, errors: list[str]
    ) -> None:
        """校验 v1.1 格式卡片：必填字段、发布闸门、内容哈希。

        错误码追加到 errors：
        - missing_field:{card_id}:{field}：必填字段缺失/为空
        - non_test_record / production_eligible_record：发布闸门未关闭
        - invalid_content_hash / content_hash_mismatch：哈希缺失或与内容不符
        """
        # 无 id 时用行号作临时标识，否则错误码会全部变成 missing:None 无法归因
        card_id = card.get("id", f"line_{line_number}")
        required = ("id", "title", "species", "category", "facts", "retrieval_text")
        for field in required:
            # 用 not card.get(field)：空字符串/空列表同样判为缺失
            # （检索器把 retrieval_text 等当文本用，空值会让卡片永远检索不到）
            if not card.get(field):
                errors.append(f"missing_field:{card_id}:{field}")
        _validate_closed_release_gate(card, card_id, errors)
        digest = card.get("content_hash")
        # 先验格式（64 位十六进制）再验值：便于区分“资产未写入哈希”与“内容被改”
        if not isinstance(digest, str) or len(digest) != 64:
            errors.append(f"invalid_content_hash:{card_id}")
        elif digest != _v11_content_hash(card):
            errors.append(f"content_hash_mismatch:{card_id}")

    @staticmethod
    def _validate_v14_card(
        card: dict[str, Any],
        line_number: int,
        source_ids: set[str],
        review_hashes: dict[str, str],
        errors: list[str],
    ) -> None:
        """校验 v1.4+ combined 格式卡片。

        在 v1.1 的基础上额外校验：
        - 必填字段含 source_supported_simple_facts 与 evidence；
        - veterinary_review.status 必须为 pending（待兽医审核）；
        - content_hash 必须与审核队列（review_hashes）中登记的一致，
          确保送审内容与线上加载内容逐字节相同；
        - 每条 evidence 的 source_id 必须存在于来源清单，且必须带
          page_or_section 定位信息（证据可溯源到具体页码/章节）。
        """
        card_id = card.get("id", f"line_{line_number}")
        # v1.4+ 把事实与证据拆成两组字段：事实是给用户看的，
        # evidence 是溯源用的（两份都要有，缺一不可）
        required = (
            "id",
            "title",
            "species",
            "category",
            "source_supported_simple_facts",
            "evidence",
            "retrieval_text",
        )
        for field in required:
            if not card.get(field):
                errors.append(f"missing_field:{card_id}:{field}")
        _validate_closed_release_gate(card, card_id, errors)
        review = card.get("veterinary_review") or {}
        # 必须是 pending：一旦状态被改成 approved 就说明该卡已进入发布流程，
        # 不应再由测试层加载器使用（发布走另一条链路）
        if review.get("status") != "pending":
            errors.append(f"unexpected_review_status:{card_id}")
        digest = card.get("content_hash")
        if not isinstance(digest, str) or len(digest) != 64:
            errors.append(f"invalid_content_hash:{card_id}")
        elif digest != _v14_content_hash(card):
            errors.append(f"content_hash_mismatch:{card_id}")
        # 双重哈希比对（与 v1.1 的关键区别）：
        #   上一条验“文件内自洽”，这一条验“文件 == 送审版”。
        # 只有两条都通过，才能证明线上加载的内容就是兽医审核过的那一份。
        if review_hashes.get(str(card_id)) != digest:
            errors.append(f"review_hash_mismatch:{card_id}")
        for evidence in card.get("evidence", []):
            source_id = evidence.get("source_id")
            if source_id not in source_ids:
                errors.append(f"unresolved_source:{card_id}:{source_id}")
            # 定位信息必需：只有 source_id 无法回答“这个事实在来源的哪一页”，
            # 而兽医复审时需要逐条回查原文
            if not evidence.get("page_or_section"):
                errors.append(f"missing_locator:{card_id}:{source_id}")

    @staticmethod
    def _load_source_ids(root: Path, asset_format: str, errors: list[str]) -> set[str]:
        """加载来源清单（sources.combined_*.json）并校验来源合法性。

        校验内容：
        - 文件缺失/JSON 损坏/类型错误 → 对应错误码；
        - 来源必须有 id，且 id 不重复；
        - v1.4~v1.7 资产要求来源允许商用（commercial_use_allowed is True），
          且许可证不得包含 NC（非商用）/ND（禁止演绎）限制。

        :return: 合法来源 ID 集合（供卡片 evidence 溯源校验）
        """
        name = {
            "v1_4": "sources.combined_v1_4.json",
            "v1_5": "sources.combined_v1_5.json",
            "v1_6": "sources.combined_v1_6.json",
            "v1_7": "sources.combined_v1_7.json",
            "v1_8": "sources.combined_v1_8.json",
        }.get(asset_format, "sources.enriched.json")
        path = root / name
        if not path.is_file():
            # 与卡片文件不同：缺来源清单是错误但可继续（后续溯源校验会全部报错）
            errors.append("sources_file_missing")
            return set()
        try:
            sources = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append("invalid_sources_json")
            return set()
        if not isinstance(sources, list):
            errors.append("invalid_sources_type")
            return set()
        source_ids: set[str] = set()
        for source in sources:
            source_id = source.get("id")
            if not source_id:
                errors.append("source_id_missing")
                continue
            if source_id in source_ids:
                # 重复 ID 会让卡片溯源指向不确定的来源，必须报错
                errors.append(f"duplicate_source:{source_id}")
            source_ids.add(source_id)
            # 版权闸门（仅 v1.4~v1.7）：v1.8 起资产策略变更，不再检查此项。
            # 三项任一不满足即拒绝：显式允许商用 / 许可证不得含 NC / 不得含 ND。
            # 注意 license 先 upper() 再判断，兼容 “CC BY-NC” 与 “cc-by-nc” 等写法。
            if asset_format in ("v1_4", "v1_5", "v1_6", "v1_7") and (
                source.get("commercial_use_allowed") is not True
                or "NC" in str(source.get("license", "")).upper()
                or "ND" in str(source.get("license", "")).upper()
            ):
                errors.append(f"disallowed_source:{source_id}")
        return source_ids

    @staticmethod
    def _read_index_version(
        root: Path, cards: list[dict[str, Any]], asset_format: str
    ) -> str:
        """读取知识库索引版本号。

        优先取 validation_report.{format}.json 中的 version/schema_version；
        报告文件不存在时退回第一张卡片的 version 字段；
        报告 JSON 损坏返回 "invalid"，无卡片返回 "empty"。
        版本号会写入 RagResult.index_version，便于线上定位资产版本。
        """
        report_name = {
            "v1_4": "validation_report.v1_4.json",
            "v1_5": "validation_report.v1_5.json",
            "v1_6": "validation_report.v1_6.json",
            "v1_7": "validation_report.v1_7.json",
            "v1_8": "validation_report.v1_8.json",
        }.get(asset_format, "validation_report.enriched.json")
        report_path = root / report_name
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                # 先取 version 再退回 schema_version：老资产只有后者
                return str(report.get("version") or report.get("schema_version") or "unknown")
            except json.JSONDecodeError:
                # 报告损坏不阻断加载：版本号仅用于观测，失败就标记为 invalid
                return "invalid"
        if not cards:
            return "empty"
        # 无报告文件时的退路：用第一张卡片自声明的版本号
        return str(cards[0].get("version") or cards[0].get("schema_version") or "unknown")

    @staticmethod
    def _validate_checksums(root: Path, errors: list[str]) -> None:
        """校验资产目录下 SHA256SUMS 清单与实际文件的完整性。

        每行格式为 "<sha256>  <filename>"（文件名可能带 * 前缀）。
        错误码：
        - checksums_file_missing：清单文件不存在；
        - invalid_checksum_line：清单行格式错误；
        - checksum_file_missing:{filename}：清单中列出的文件缺失；
        - checksum_mismatch:{filename}：文件实际哈希与清单不符（被篡改/损坏）。
        """
        sums_path = root / "SHA256SUMS"
        if not sums_path.is_file():
            errors.append("checksums_file_missing")
            return
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            # maxsplit=1：文件名里可能含空格，只切第一段作为哈希
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                errors.append("invalid_checksum_line")
                continue
            expected, filename = parts
            # lstrip("*")：兼容 sha256sum 在二进制模式输出时给文件名加的 * 前缀
            file_path = root / filename.lstrip("*")
            if not file_path.is_file():
                # 清单里有但文件不在：半包上传/漏拷文件
                errors.append(f"checksum_file_missing:{filename}")
            elif asset_file_digest(file_path) != expected.lower():
                # 两侧统一小写比较（sha256sum 输出小写，人工编辑可能写大写）
                errors.append(f"checksum_mismatch:{filename}")


def asset_file_digest(path: Path) -> str:
    """计算文件的 SHA-256 十六进制摘要（用于 SHA256SUMS 完整性校验）。

    注意与 _v14_content_hash 的区别：
    本函数算的是“字节级文件哈希”（验传输/存储完整性），
    后者算的是“字段级语义哈希”（验内容是否被编辑）。
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_review_queue(root: Path, asset_format: str = "v1_4") -> dict[str, str]:
    """读取兽医审核队列 CSV，返回 {记录 ID: content_hash} 映射。

    用于审核一致性检查：卡片/急症规则文件中登记的 content_hash 必须与
    审核队列中登记的完全一致，证明"送审版本 == 加载版本"。
    文件不存在时返回空字典（不视为错误，由调用方决定后续校验）。

    注意：v1_8 队列文件名沿用 combined_ 前缀（veterinary_review_queue.combined_v1_8.csv）。

    【为什么文件缺失不算错误】
    队列是“送审流程的产物”，而不是运行必需资产：
    缺失时返回空字典，_validate_v14_card 会因 review_hashes.get() 为 None
    而在每张卡上报 review_hash_mismatch，从而进入 ready=False，
    最终效果与报错一致，但失败原因更具体（能看出是哈希对不上而非文件不存在）。
    """
    path = root / f"veterinary_review_queue.combined_{asset_format}.csv"
    if not path.is_file():
        return {}
    # newline=""：交给 csv 模块处理换行，避免 CRLF 行尾的残留回车符混入字段值
    with path.open(encoding="utf-8", newline="") as handle:
        # 当前实现假定 CSV 含 id / content_hash 两列；
        # 缺列会抛 KeyError（属于资产格式错误，直接暴露比静默更好）
        return {row["id"]: row["content_hash"] for row in csv.DictReader(handle)}


def _validate_closed_release_gate(
    record: dict[str, Any], record_id: str, errors: list[str]
) -> None:
    """发布闸门校验：测试层资产必须显式标记为"未发布"。

    - index_tier 必须等于 "test"（否则记录 non_test_record）；
    - production_eligible 必须显式为 False（否则记录 production_eligible_record）。

    这是防止未经审核的知识卡片误入生产链路的最后一道静态防线。
    """
    # 用 “!=” 而非 “not in”：空值/拼写错误（如 "test "带空格）都算闸门未关闭
    if record.get("index_tier") != "test":
        errors.append(f"non_test_record:{record_id}")
    # is not False：必须显式为布尔 False；缺字段（None）也算未关闭闸门，
    # 避免“没写这个字段”被当作“未发布”而放行
    if record.get("production_eligible") is not False:
        errors.append(f"production_eligible_record:{record_id}")


def _normalize_v14_card(card: dict[str, Any]) -> dict[str, Any]:
    """把 v1.4+ combined 卡片归一化为检索器统一使用的内部结构。

    转换内容：
    - source_supported_simple_facts（字符串列表）→ facts（对象列表），
      每个 fact 带稳定 ID（"{card_id}:fact-{n}"）、文本、证据引用和
      映射状态标记（source_checked_v1_4_veterinary_review_pending）；
    - evidence 列表 → 每个 fact 共享的 evidence_refs（来源/定位/核验状态）；
    - safe_next_step（单条）→ safe_next_steps（列表）；
    - veterinary_review → review 字段。

    归一化后检索器无需区分 v1.1 与 v1.4+ 格式。

    【为什么用 dict(card) 浅拷贝而不是改原字典】
    原始卡片对象仍会被上层引用（如需导出/对比），原地修改会造成“送审内容”
    与内存中对象不一致的错觉；归一化只应产生新视图。

    【为什么每个 fact 都共享同一份 evidence_refs 对象】
    combined 资产的证据是卡片级的（一组 evidence 支撑整卡所有事实），
    因此每个 fact 引用同一列表。下游只读，不存在互相污染风险。

    【mapping_status 为何写死】
    归一化产物不能被当成“已核验”；写死该字符串保证任何下游
    都不会把它误认为已完成兽医核验（核验状态以 veterinary_review 为准）。
    """
    normalized = dict(card)
    # 先抽取出对下游有用的三个字段，丢弃其余审核元数据（不进 prompt）
    evidence_refs = [
        {
            "source_id": evidence.get("source_id"),
            "page_or_section": evidence.get("page_or_section"),
            "verification_status": evidence.get("verification_status"),
        }
        for evidence in card.get("evidence", [])
    ]
    # enumerate(..., 1)：事实编号从 1 开始（与人工阅读习惯一致），
    # 且 ID 稳定（同一个卡片同一条事实永远得到相同 ID），可直接用于评估对齐
    normalized["facts"] = [
        {
            "id": f"{card['id']}:fact-{index}",
            "text": text,
            "evidence_refs": evidence_refs,
            "mapping_status": "source_checked_v1_4_veterinary_review_pending",
        }
        for index, text in enumerate(card.get("source_supported_simple_facts", []), 1)
    ]
    # 单值字段转列表：检索器/生成侧统一按列表处理，避免两套分支
    normalized["safe_next_steps"] = [card["safe_next_step"]] if card.get("safe_next_step") else []
    # 审核字段改名：v1.4+ 叫 veterinary_review，v1.1 叫 review
    normalized["review"] = card.get("veterinary_review", {})
    return normalized


def _v14_content_hash(record: dict[str, Any]) -> str:
    """计算 v1.4+ 记录的内容哈希：对除 content_hash 外的全部字段做
    紧凑 JSON 序列化（不排序键，保持资产产出顺序）后取 SHA-256。

    用于检测卡片/急症规则文件在送审后是否被改动。

    【为什么与 emergency_shadow._content_hash 完全一样】
    两者必须同算法，否则同一份规则在“卡片校验”与“急症校验”两处
    会算出不同哈希而互相矛盾。修改任一处都必须同步另一处。

    【为什么 serialize 不排序键、而 v1.1 排序】
    v1.4+ 的哈希基线由生产流程生成并登记在审核队列中，
    算法必须与“生成方”逐字节一致，因此只能完全照抄当时的行为（不排序）。
    """
    payload = {key: value for key, value in record.items() if key != "content_hash"}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _v11_content_hash(card: dict[str, Any]) -> str:
    """计算 v1.1 卡片的内容哈希：只对核心业务字段（忽略元数据噪音），
    按固定键序排序后做紧凑 JSON 序列化，再取 SHA-256。

    固定 core_fields 白名单保证：非业务字段（如审核备注）变化不会导致
    哈希不匹配，而任何业务内容改动都能被检出。

    【与 v1.4 哈希策略的差异及原因】
    v1.1 是早期资产，没有“送审基线哈希”可对齐，因此可以选择更稳健的
    白名单 + 排序方案：把审核元数据（如 review 备注、时间戳）排除在外，
    避免“只改了备注就报哈希不符”的伪阳性。

    注意：core_fields 必须与资产产出方保持一致；新增业务字段时
    若不同步加进白名单，该字段的改动将无法被哈希检出。
    """
    core_fields = (
        "id",
        "version",
        "locale",
        "title",
        "species",
        "category",
        "scope",
        "user_phrases",
        "owner_observable_signs",
        "facts",
        "questions_to_ask",
        "safe_next_steps",
        "contraindications",
        "base_risk",
        "red_flags",
        "policy_tags",
        "retrieval_text",
        "commercial_use_allowed",
    )
    payload = {field: card.get(field) for field in core_fields}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
