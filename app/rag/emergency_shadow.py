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

【在系统中的位置（调用链）】
- 构造：app/core/dependencies.py 在开关 rag_emergency_shadow 打开时调用
  V14EmergencyShadowMatcher.load(...) 并把实例注入 ConsultAgent；
- 调用：ConsultAgent 步骤 4 调用 search()，只把 matched_rule_ids / top_severity
  写进日志（事件名 rag_emergency_shadow_result）；
- 异常：调用点整体 try/except 吞异常只记 warning，因此本模块运行期出错
  不会影响本次问诊。“影子”的硬约束就是——不要向上层抛异常；
- 复用：scripts/validate_rag_assets.py（离线资产校验）与
  scripts/evaluate_rag_shadow.py（离线命中评估）也复用同一个 load()。
  即修改 load() 的校验口径会同时影响线上观测与离线评估，必须同步回归。

【与线上急症引擎的边界（改动前必读）】
真实分诊由 app/safety/emergency_rules.py 的 EmergencyRuleEngine 负责。
本模块处理的是同一批规则的“下一版本候选”，用影子方式先在真实流量上
观察命中情况，为规则转正积累证据。因此：
- 依赖方向是单向的，emergency_rules.py 不 import 本文件；
- search() 的返回值只能写日志/遥测，绝不可写入 ConsultState 的急症判定字段。
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
#
# 【正则拆解】
#   第 1 段 否定词表（长词在前，靠正则回溯同时兼容短词）；
#   第 2 段 否定词与触发词之间最多 6 个“非句读”字符，且以 prefix 结尾
#          （结尾锚定保证中间不被逗号/句号隔断）。
#
# 【与 _trigger_matches 的 14 字符窗口的关系】
#   14 只是前缀截断窗口上限（否定词最长 4 字 + 中间 6 字 + 余量），
#   真正生效的判据是“否定词紧邻触发词且中间无句读”。因此：
#     - “没有呕吐，但是现在腹泻” → 逗号隔断，“腹泻”照常命中；
#     - “没有呕吐”                → “呕吐”被正确判为否定语境。
#
# 【为什么不做“后置否定”】
#   中文否定基本前置（“不吐”而非“吐不”），只为前置否定建规则可保持低误伤。
#   代价是“呼吸困难？没有”这类倒装会漏判——影子场景可接受（漏拦只影响
#   观测统计，不影响线上分诊）。
_NEGATED_BEFORE_RE = re.compile(
    r"(?:没有|没|无|未|否认|并无|不存在|不是|不再|未见|未出现|没有出现|没出现)"
    r"[^，。；！？]{0,6}$"
)


@dataclass(frozen=True)
class EmergencyShadowReport:
    """急症规则资产加载报告（不可变）。

    :param rules: 规则元组（与 JSON 文件内容一一对应，校验失败**不过滤**条目，
                  仅把错误码记入 errors；可靠性统一由 ready 保证，与 loader 同思路）
    :param version: 规则版本号
    :param errors: 校验错误码元组；非空表示资产不可用于影子匹配
    """

    rules: tuple[dict[str, Any], ...]
    version: str
    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """资产是否就绪：有规则且无校验错误（与 loader.RagAssetReport.ready 同口径）。

        注意 search() 以此为准做整体早退：只要 errors 非空就完全不匹配。
        这同时是下面 index 访问安全的隐含前提——有错时不进入匹配循环，
        因此缺 id 的规则不会被下标取用。若将来改成“逐条降级匹配”，
        必须先把 item["id"] 的取值方式一并改掉。
        """
        return bool(self.rules) and not self.errors


@dataclass(frozen=True)
class EmergencyShadowResult:
    """一次影子匹配的结果（只用于日志/对比，不影响线上）。

    :param matched_rule_ids: 命中的规则 ID 元组（已按严重度排序，可复现）
    :param severities: 命中规则的严重度元组（已去重、按优先级排序）
    :param top_severity: 最高严重度（emergency_now 优先）；无命中为 None。
                         它恒等于 severities[0]，是给日志/看板单独取用的便利字段
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

    使用约束：
    - load() 不抛异常，matcher 一定能构造成功；report.ready 为 False 时
      search() 恒返回空结果，因此调用方不必判 None、也不必包 try/except；
    - 是否启用本能力由装配开关决定（见 dependencies），与构造函数无关。
    """

    def __init__(self, report: EmergencyShadowReport):
        """:param report: load() 产出的急症规则资产报告"""
        self.report = report

    @classmethod
    def load(cls, asset_root: str | Path, asset_format: str = "v1_4") -> "V14EmergencyShadowMatcher":
        """从资产目录加载并校验急症规则文件。

        文件缺失/JSON 损坏/类型错误/任何发布闸门或哈希校验失败时，
        都返回带 errors 的报告（ready=False），不抛异常。

        【三类早退错误码（都发生在逐条校验之前）】
        - emergency_rules_missing      → 文件不存在（含“版本参数认不出”的兜底情形）
        - invalid_emergency_rules_json → JSON 解析失败（文件损坏/半包上传）
        - invalid_emergency_rules_type → 顶层不是数组（结构完全不对）
        这三类都属于“根本读不到数据”，无法逐条报错，所以直接整包早退。

        【逐条校验的错误码】duplicate_ / missing_ / invalid_emergency_severity /
        emergency_release_gate_open / unexpected_emergency_review_status /
        emergency_content_hash_mismatch / emergency_review_hash_mismatch，
        命名与 loader.py 同一套规范，便于日志聚合。

        :param asset_root: 资产根目录（如 assets/rag/v1_8）
        :param asset_format: 资产版本（v1_4~v1_8）
        """
        root = Path(asset_root)
        # 文件名按版本映射；.get 的兜底值刻意也指向 v1_4 的文件名：
        # 若调用方传了未知版本，会表现为 "文件不存在"（missing）而不是
        # KeyError 崩溃——把“参数写错”统一成可观测的资产缺失。
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
            # 早退分支 1：文件不在（版本目录不对/未随包发布）
            return cls(EmergencyShadowReport((), "unavailable", ("emergency_rules_missing",)))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 早退分支 2：能读到文件但解析不出 JSON
            return cls(EmergencyShadowReport((), "invalid", ("invalid_emergency_rules_json",)))
        if not isinstance(payload, list):
            # 早退分支 3：能解析但不是顶层数组（如被包成 {"rules": [...]}）
            return cls(EmergencyShadowReport((), "invalid", ("invalid_emergency_rules_type",)))

        # 读取兽医审核队列登记的内容哈希（送审版本基线）
        review_hashes = read_review_queue(root, asset_format)
        # 已出现过的 rule_id；用于报重复。注意 add 放在判断之后、无条件执行。
        seen: set[str] = set()
        for rule in payload:
            # 默认值 "missing" 而不取 None：保证错误码里总是可读字符串
            rule_id = rule.get("id", "missing")
            # ⑦-1 规则 ID 不得重复（重复会导致日志/top_severity 归因不清）
            if rule_id in seen:
                errors.append(f"duplicate_emergency_rule:{rule_id}")
            seen.add(rule_id)
            # ⑦-2 必填字段齐全性。用“真值”而非“键存在”判断，因此
            # triggers=[] / action="" 这类空值同样会被计为缺失——
            # 有意为之：空规则等价于无规则，不应进入影子匹配。
            if not all(rule.get(field) for field in ("id", "species", "severity", "triggers", "action")):
                errors.append(f"missing_emergency_field:{rule_id}")
            # ⑦-3 严重度白名单：只允许两级。新增级别需要同步改这里的白名单
            # 以及 search() 排序键里写死的 emergency_now（见下方注释）。
            if rule.get("severity") not in {"emergency_now", "urgent_same_day"}:
                errors.append(f"invalid_emergency_severity:{rule_id}")
            # ⑦-4 发布闸门：必须仍处于测试层、未标记可上线（与 loader
            # _validate_closed_release_gate 同一判据）。注意用 is not False：
            # 字段缺失也视为“闸门未关闭”。
            if rule.get("index_tier") != "test" or rule.get("production_eligible") is not False:
                errors.append(f"emergency_release_gate_open:{rule_id}")
            # ⑦-5 兽医审核状态必须为 pending（待审核）；or {} 兼容字段缺失
            if (rule.get("veterinary_review") or {}).get("status") != "pending":
                errors.append(f"unexpected_emergency_review_status:{rule_id}")
            # ⑦-6 内容哈希：与文件内登记值一致（防篡改）。
            #      digest 只算一次，下面两个比对共用，避免重复序列化开销。
            digest = _content_hash(rule)
            if rule.get("content_hash") != digest:
                errors.append(f"emergency_content_hash_mismatch:{rule_id}")
            # ⑦-7 内容哈希：与审核队列登记值一致（保证“送审版本 == 加载版本”）。
            #      队列文件缺失时 review_hashes 为空字典，此处会逐条报
            #      review_hash_mismatch——错误定位比笼统的“队列缺失”更具体。
            if review_hashes.get(rule_id) != digest:
                errors.append(f"emergency_review_hash_mismatch:{rule_id}")

        # 版本号取首条规则的 version 字段（约定同一资产包内所有规则共享同一版本）。
        # 空数组给 "empty"：此时 rules 为空，ready 仍为 False，语义不会歧义。
        version = str(payload[0].get("version", "unknown")) if payload else "empty"
        # 注意放入报告的是**全部** payload（不过滤已报错的条目），
        # 与 loader 一致：错误信息与规则列表分离，是否可用统一看 ready。
        # dict.fromkeys 对错误码保序去重
        return cls(EmergencyShadowReport(tuple(payload), version, tuple(dict.fromkeys(errors))))

    def search(self, text: str, *, species: str | None = None) -> EmergencyShadowResult:
        """对用户文本执行急症规则影子匹配（结果不影响线上分诊）。

        :param text: 用户问诊文本
        :param species: 物种（cat/dog/None）；无法归一（None）时**不做物种
                        过滤**，全部规则都参与匹配（详见下方注释）
        :return: EmergencyShadowResult（命中规则 ID、严重度列表、最高严重度）；
                 资产未就绪或文本为空时返回空结果
        """
        # 早退：资产不可用 / 空文本 → 返回空结果而不抛异常。
        # 空结果与“未命中”在类型上无法区分，但两者对线上都是“无事发生”，
        # 且调用方只看 matched_rule_ids / top_severity，无需区分。
        if not self.report.ready or not text:
            return EmergencyShadowResult((), (), None)
        # 物种归一化（“猫”/“猫咪”/“cat” → "cat"），与检索器同一套映射
        normalized_species = normalize_species(species)
        matches: list[dict[str, Any]] = []
        for rule in self.report.rules:
            # 物种过滤：规则声明的物种集合需包含当前物种。
            # 规则内可能写中文（“猫”），因此逐项再做一次 normalize 后比集合。
            allowed_species = {normalize_species(value) for value in rule.get("species", [])}
            # normalized_species 为空时**不过滤**：影子场景下多记一次命中
            # 只是多一条日志，却能避免因物种缺失而整批漏采。
            if normalized_species and normalized_species not in allowed_species:
                continue
            # 任一 trigger 短语命中（且非否定语境）即视为该规则命中。
            # any 短路：triggers 顺序只影响“先尝试哪个 trigger”，
            # 不影响结果（只要有一命中就是命中）。
            if any(_trigger_matches(text, trigger) for trigger in rule.get("triggers", [])):
                matches.append(rule)
        # 排序：emergency_now（立即急诊）优先，其次按规则 ID 稳定排序。
        # key 第一项是布尔值（False 排前），所以需要立即急救的规则总在前面；
        # 第二项 id 保证同严重度下顺序可复现（日志 diff 不会因字典序波动）。
        # 这里用下标而非 get 是安全的：report.ready 已保证无校验错误，
        # 而必填字段校验覆盖了 id / severity。
        matches.sort(key=lambda item: (item.get("severity") != "emergency_now", item["id"]))
        # 去重保留首次出现顺序（即已排好序的顺序），因此 severities[0]
        # 必然就是最高严重度
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

    【为什么要循环扫描而不是 find 一次】
    同一条 trigger 可能在文本中出现多次，且第一次可能出现于否定语境、
    第二次才是肯定陈述（如“之前没呕吐，今天早上呕吐了”）。若只判定
    第一次出现就会漏报真实急症，因此必须遍历全部出现位置。

    【前缀窗口的两个细节】
    - max(0, index - 14)：靠前的位置取不到 14 字符，防负索引；
    - start = index + len(trigger)：从本次命中之后继续找，不重叠推进。
      后果是“与前次重合的 trigger 出现”会被跳过（如 abcab 中第二个 ab），
      但急症 trigger 多为 2~6 字短语，此损耗可忽略。
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
    取 SHA-256（与 loader._v14_content_hash 同一算法）。

    【必须与 loader._v14_content_hash 逐字节一致】
    两处比对的是同一个 content_hash 字段：生产方写入哪个，这里就必须
    能用同样的方式复现，否则全部规则都会报 content_hash_mismatch。
    因此以下三点都不能单独改：
      - 剔除的字段名（"content_hash"）；
      - ensure_ascii=False（保留中文原字符）；
      - separators=(",", ":")（无空格紧凑输出）。
    另外这里**不排序键**：字段顺序即生产方写入顺序，排序会得到不同
    字节流（与 _v11_content_hash 的排序策略差异及原因见 loader.py）。
    """
    payload = {key: value for key, value in record.items() if key != "content_hash"}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
