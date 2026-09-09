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
        """
        if self.asset_path is None:
            return RagAssetReport((), frozenset(), "unconfigured", ("asset_path_missing",))

        root, cards_path, asset_format = self._resolve_paths(self.asset_path)
        if cards_path is None or not cards_path.is_file():
            return RagAssetReport(
                (), frozenset(), "unavailable", ("cards_file_missing",), asset_format
            )

        errors: list[str] = []
        if asset_format in self._V14_LIKE:
            self._validate_checksums(root, errors)

        raw_cards = self._read_cards(cards_path, errors)
        source_ids = self._load_source_ids(root, asset_format, errors)
        review_hashes = (
            read_review_queue(root, asset_format) if asset_format in self._V14_LIKE else {}
        )
        cards: list[dict[str, Any]] = []
        for line_number, raw_card in raw_cards:
            if asset_format in self._V14_LIKE:
                self._validate_v14_card(
                    raw_card, line_number, source_ids, review_hashes, errors
                )
                cards.append(_normalize_v14_card(raw_card))
            else:
                self._validate_v11_card(raw_card, line_number, errors)
                cards.append(raw_card)

        if asset_format == "v1_1":
            for card in cards:
                for fact in card.get("facts", []):
                    for ref in fact.get("evidence_refs", []):
                        source_id = ref.get("source_id")
                        if source_id not in source_ids:
                            errors.append(f"unresolved_source:{card.get('id')}:{source_id}")

        version = self._read_index_version(root, cards, asset_format)
        return RagAssetReport(
            tuple(cards),
            frozenset(source_ids),
            version,
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
        if path.is_file():
            for fmt, name in (("v1_4", cls._V14_CARDS), ("v1_5", cls._V15_CARDS),
                              ("v1_6", cls._V16_CARDS), ("v1_7", cls._V17_CARDS),
                              ("v1_8", cls._V18_CARDS)):
                if path.name == name:
                    return path.parent, path, fmt
            return path.parent, path, "v1_1"
        for fmt, name in (("v1_4", cls._V14_CARDS), ("v1_5", cls._V15_CARDS),
                          ("v1_6", cls._V16_CARDS), ("v1_7", cls._V17_CARDS),
                          ("v1_8", cls._V18_CARDS)):
            candidate = path / name
            if candidate.is_file():
                return path, candidate, fmt
        v11 = path / cls._V11_CARDS
        if v11.is_file():
            return path, v11, "v1_1"
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
        for line_number, line in enumerate(cards_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                card = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"invalid_json_line:{line_number}")
                continue
            if not isinstance(card, dict):
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
        card_id = card.get("id", f"line_{line_number}")
        required = ("id", "title", "species", "category", "facts", "retrieval_text")
        for field in required:
            if not card.get(field):
                errors.append(f"missing_field:{card_id}:{field}")
        _validate_closed_release_gate(card, card_id, errors)
        digest = card.get("content_hash")
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
        if review.get("status") != "pending":
            errors.append(f"unexpected_review_status:{card_id}")
        digest = card.get("content_hash")
        if not isinstance(digest, str) or len(digest) != 64:
            errors.append(f"invalid_content_hash:{card_id}")
        elif digest != _v14_content_hash(card):
            errors.append(f"content_hash_mismatch:{card_id}")
        if review_hashes.get(str(card_id)) != digest:
            errors.append(f"review_hash_mismatch:{card_id}")
        for evidence in card.get("evidence", []):
            source_id = evidence.get("source_id")
            if source_id not in source_ids:
                errors.append(f"unresolved_source:{card_id}:{source_id}")
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
                errors.append(f"duplicate_source:{source_id}")
            source_ids.add(source_id)
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
                return str(report.get("version") or report.get("schema_version") or "unknown")
            except json.JSONDecodeError:
                return "invalid"
        if not cards:
            return "empty"
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
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                errors.append("invalid_checksum_line")
                continue
            expected, filename = parts
            file_path = root / filename.lstrip("*")
            if not file_path.is_file():
                errors.append(f"checksum_file_missing:{filename}")
            elif asset_file_digest(file_path) != expected.lower():
                errors.append(f"checksum_mismatch:{filename}")


def asset_file_digest(path: Path) -> str:
    """计算文件的 SHA-256 十六进制摘要（用于 SHA256SUMS 完整性校验）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_review_queue(root: Path, asset_format: str = "v1_4") -> dict[str, str]:
    """读取兽医审核队列 CSV，返回 {记录 ID: content_hash} 映射。

    用于审核一致性检查：卡片/急症规则文件中登记的 content_hash 必须与
    审核队列中登记的完全一致，证明"送审版本 == 加载版本"。
    文件不存在时返回空字典（不视为错误，由调用方决定后续校验）。

    注意：v1_8 队列文件名沿用 combined_ 前缀（veterinary_review_queue.combined_v1_8.csv）。
    """
    path = root / f"veterinary_review_queue.combined_{asset_format}.csv"
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as handle:
        return {row["id"]: row["content_hash"] for row in csv.DictReader(handle)}


def _validate_closed_release_gate(
    record: dict[str, Any], record_id: str, errors: list[str]
) -> None:
    """发布闸门校验：测试层资产必须显式标记为"未发布"。

    - index_tier 必须等于 "test"（否则记录 non_test_record）；
    - production_eligible 必须显式为 False（否则记录 production_eligible_record）。

    这是防止未经审核的知识卡片误入生产链路的最后一道静态防线。
    """
    if record.get("index_tier") != "test":
        errors.append(f"non_test_record:{record_id}")
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
    """
    normalized = dict(card)
    evidence_refs = [
        {
            "source_id": evidence.get("source_id"),
            "page_or_section": evidence.get("page_or_section"),
            "verification_status": evidence.get("verification_status"),
        }
        for evidence in card.get("evidence", [])
    ]
    normalized["facts"] = [
        {
            "id": f"{card['id']}:fact-{index}",
            "text": text,
            "evidence_refs": evidence_refs,
            "mapping_status": "source_checked_v1_4_veterinary_review_pending",
        }
        for index, text in enumerate(card.get("source_supported_simple_facts", []), 1)
    ]
    normalized["safe_next_steps"] = [card["safe_next_step"]] if card.get("safe_next_step") else []
    normalized["review"] = card.get("veterinary_review", {})
    return normalized


def _v14_content_hash(record: dict[str, Any]) -> str:
    """计算 v1.4+ 记录的内容哈希：对除 content_hash 外的全部字段做
    紧凑 JSON 序列化（不排序键，保持资产产出顺序）后取 SHA-256。

    用于检测卡片/急症规则文件在送审后是否被改动。
    """
    payload = {key: value for key, value in record.items() if key != "content_hash"}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _v11_content_hash(card: dict[str, Any]) -> str:
    """计算 v1.1 卡片的内容哈希：只对核心业务字段（忽略元数据噪音），
    按固定键序排序后做紧凑 JSON 序列化，再取 SHA-256。

    固定 core_fields 白名单保证：非业务字段（如审核备注）变化不会导致
    哈希不匹配，而任何业务内容改动都能被检出。
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
