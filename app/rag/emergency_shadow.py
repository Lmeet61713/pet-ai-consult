"""急症规则"影子匹配器"（Emergency Shadow Matcher）。

【核心定位】
对待发布的 v1.4~v1.8 急症规则资产（emergency_rules.combined_*.json）进行
只加载、只验证、只记录的"影子运行"：
- 影子：匹配结果只写日志/遥测，**绝不参与线上分诊决策**（线上急症判定仍由
  app/safety/emergency_rules.py 的正式规则引擎负责）；
- 目的：在真实流量上观察新急症规则的命中情况与准确率，为规则灰度发布
  积累证据；同时强制校验资产完整性，防止不合格规则流入发布流程。

【加载时校验（与 loader.py 同一套发布闸门思路）】
- 规则 ID 不重复；必填字段（id/species/severity/triggers/action）齐全；
- severity 只允许 emergency_now（立即急诊）/ urgent_same_day（当日紧急）；
- index_tier 必须为 test、production_eligible 必须为 False（闸门未关闭即报错）；
- veterinary_review.status 必须为 pending；
- content_hash 与重算值一致，且与兽医审核队列登记的哈希一致。

【匹配逻辑】
search() 按物种过滤后，用 triggers 短语做子串匹配（支持否定语境排除），
命中规则按严重度排序（emergency_now 优先），返回命中 ID 与最高严重度。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.rag.loader import read_review_queue
from app.rag.retriever import normalize_species

# 否定语境正则：触发词前 14 个字符内若出现"没有/未/否认/不是…"等否定，
# 则该次命中不计（如"没有呼吸困难"不应触发急症规则）。
_NEGATED_BEFORE_RE = re.compile(
    r"(?:没有|没|无|未|否认|并无|不存在|不是|不再|未见|未出现|没有出现|没出现)"
    r"[^，。；！？]{0,6}$"
)


@dataclass(frozen=True)
class EmergencyShadowReport:
    """急症规则资产加载报告（不可变）。

    :param rules: 校验通过的急症规则元组
    :param version: 规则版本号
    :param errors: 校验错误码元组；非空表示资产不可用于影子匹配
    """

    rules: tuple[dict[str, Any], ...]
    version: str
    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """资产是否就绪：有规则且无校验错误。"""
        return bool(self.rules) and not self.errors


@dataclass(frozen=True)
class EmergencyShadowResult:
    """一次影子匹配的结果（只用于日志/对比，不影响线上）。

    :param matched_rule_ids: 命中的规则 ID 元组
    :param severities: 命中规则的严重度元组（已去重、按优先级排序）
    :param top_severity: 最高严重度（emergency_now 优先）；无命中为 None
    """

    matched_rule_ids: tuple[str, ...]
    severities: tuple[str, ...]
    top_severity: str | None


class V14EmergencyShadowMatcher:
    """v1.4+ 急症规则影子匹配器：验证并试跑待发布规则，不影响线上分诊。

    典型用法：
        matcher = V14EmergencyShadowMatcher.load("assets/rag/v1_8", "v1_8")
        result = matcher.search(user_text, species="cat")
        # result 仅写入日志/遥测，不改变 ConsultState 的急症判定
    """

    def __init__(self, report: EmergencyShadowReport):
        """:param report: load() 产出的急症规则资产报告"""
        self.report = report

    @classmethod
    def load(cls, asset_root: str | Path, asset_format: str = "v1_4") -> "V14EmergencyShadowMatcher":
        """从资产目录加载并校验急症规则文件。

        文件缺失/JSON 损坏/类型错误/任何发布闸门或哈希校验失败时，
        都返回带 errors 的报告（ready=False），不抛异常。

        :param asset_root: 资产根目录（如 assets/rag/v1_8）
        :param asset_format: 资产版本（v1_4~v1_8）
        """
        root = Path(asset_root)
        filename = {
            "v1_4": "emergency_rules.combined_v1_4.json",
            "v1_5": "emergency_rules.combined_v1_5.json",
            "v1_6": "emergency_rules.combined_v1_6.json",
            "v1_7": "emergency_rules.combined_v1_7.json",
            "v1_8": "emergency_rules.combined_v1_8.json",
        }.get(asset_format, "emergency_rules.combined_v1_4.json")
        path = root / filename
        errors: list[str] = []
        if not path.is_file():
            return cls(EmergencyShadowReport((), "unavailable", ("emergency_rules_missing",)))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return cls(EmergencyShadowReport((), "invalid", ("invalid_emergency_rules_json",)))
        if not isinstance(payload, list):
            return cls(EmergencyShadowReport((), "invalid", ("invalid_emergency_rules_type",)))

        # 读取兽医审核队列登记的内容哈希（送审版本基线）
        review_hashes = read_review_queue(root, asset_format)
        seen: set[str] = set()
        for rule in payload:
            rule_id = rule.get("id", "missing")
            # 规则 ID 不得重复
            if rule_id in seen:
                errors.append(f"duplicate_emergency_rule:{rule_id}")
            seen.add(rule_id)
            # 必填字段齐全性检查
            if not all(rule.get(field) for field in ("id", "species", "severity", "triggers", "action")):
                errors.append(f"missing_emergency_field:{rule_id}")
            # 严重度白名单：只允许两级
            if rule.get("severity") not in {"emergency_now", "urgent_same_day"}:
                errors.append(f"invalid_emergency_severity:{rule_id}")
            # 发布闸门：必须仍处于测试层、未标记可上线
            if rule.get("index_tier") != "test" or rule.get("production_eligible") is not False:
                errors.append(f"emergency_release_gate_open:{rule_id}")
            # 兽医审核状态必须为 pending（待审核）
            if (rule.get("veterinary_review") or {}).get("status") != "pending":
                errors.append(f"unexpected_emergency_review_status:{rule_id}")
            # 内容哈希：与文件内登记值一致（防篡改）
            digest = _content_hash(rule)
            if rule.get("content_hash") != digest:
                errors.append(f"emergency_content_hash_mismatch:{rule_id}")
            # 内容哈希：与审核队列登记值一致（保证送审版本 == 加载版本）
            if review_hashes.get(rule_id) != digest:
                errors.append(f"emergency_review_hash_mismatch:{rule_id}")

        version = str(payload[0].get("version", "unknown")) if payload else "empty"
        # dict.fromkeys 对错误码保序去重
        return cls(EmergencyShadowReport(tuple(payload), version, tuple(dict.fromkeys(errors))))

    def search(self, text: str, *, species: str | None = None) -> EmergencyShadowResult:
        """对用户文本执行急症规则影子匹配（结果不影响线上分诊）。

        :param text: 用户问诊文本
        :param species: 物种（cat/dog/None）；未知物种时只匹配物种字段
                        兼容该情况的规则（由规则自身 species 列表决定）
        :return: EmergencyShadowResult（命中规则 ID、严重度列表、最高严重度）；
                 资产未就绪或文本为空时返回空结果
        """
        if not self.report.ready or not text:
            return EmergencyShadowResult((), (), None)
        normalized_species = normalize_species(species)
        matches: list[dict[str, Any]] = []
        for rule in self.report.rules:
            # 物种过滤：规则声明的物种集合需包含当前物种
            allowed_species = {normalize_species(value) for value in rule.get("species", [])}
            if normalized_species and normalized_species not in allowed_species:
                continue
            # 任一 trigger 短语命中（且非否定语境）即视为该规则命中
            if any(_trigger_matches(text, trigger) for trigger in rule.get("triggers", [])):
                matches.append(rule)
        # 排序：emergency_now（立即急诊）优先，其次按规则 ID 稳定排序
        matches.sort(key=lambda item: (item.get("severity") != "emergency_now", item["id"]))
        severities = tuple(dict.fromkeys(str(rule["severity"]) for rule in matches))
        return EmergencyShadowResult(
            tuple(rule["id"] for rule in matches),
            severities,
            severities[0] if severities else None,
        )


def _trigger_matches(text: str, trigger: str) -> bool:
    """判断 trigger 短语是否在 text 中出现且不处于否定语境。

    扫描 trigger 的所有出现位置；只要有一处前面 14 个字符内
    没有否定表达（_NEGATED_BEFORE_RE），即判定命中。
    """
    start = 0
    while (index := text.find(trigger, start)) >= 0:
        prefix = text[max(0, index - 14) : index]
        if _NEGATED_BEFORE_RE.search(prefix) is None:
            return True
        start = index + len(trigger)
    return False


def _content_hash(record: dict[str, Any]) -> str:
    """计算急症规则的内容哈希：剔除 content_hash 字段后紧凑 JSON 序列化，
    取 SHA-256（与 loader._v14_content_hash 同一算法）。"""
    payload = {key: value for key, value in record.items() if key != "content_hash"}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
