"""药物与剂量规则（v5 §13.3：拦截具体剂量 / 高风险人用药，拦截率目标 100%）"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.core.config import CONFIG_DIR

# 剂量信号：数字 + 剂量单位（mg/ml/粒/片/滴/毫升/毫克/微克…）
# 单独的 g/kg 不直接算（可能是体重/食物），需要用药语境
_DOSE_UNIT_RE = re.compile(
    r"\d+(\.\d+)?\s*(mg|ml|ug|微克|毫克|毫升|粒|片|滴|滴剂)"
    r"|\d+\s*(克|g)(?=[^。]{0,12}(每|按|体重|一次|每天|每日|喂|服|用))"
)

# 频率/疗程信号（需结合用药语境：刷牙/洗澡等日常护理的"每天一次"不算用药）
# 支持阿拉伯/中文数字与常见量词（"每天喂药两次""每日1片"）
_DOSE_FREQ_RE = re.compile(
    r"(每天|每日|一日|一天|每顿|每次|疗程|连吃|服用)"
    r"(喂药|服药|吃药|用药|涂药|喂|服|吃|用|涂|抹|滴|注射|口服)?"
    r"\s*[0-9一二两三四五六七八九十半]+\s*(次|顿|片|粒|毫升|ml|滴|剂)?"
)
_MEDICATION_CONTEXT_RE = re.compile(r"药|用药|服药|喂药|吃药|抗生素|消炎|药物|片|粒|剂量|针剂|口服液")

# 具体药物名（宠物兽药常见）
_VET_DRUG_RE = re.compile(
    r"(阿莫西林克拉维酸|多西环素|拜有利|恩诺沙星|甲硝唑|灭滴灵|伊曲康唑|"
    r"泼尼松|地塞米松|呋塞米|利尿剂|溴化钾|加巴喷丁|美洛昔康|卡洛芬|"
    r"尼可刹米|强心苷|地高辛)"
)

_SAFE_MENTION_RE = re.compile(
    r"误食|吃了|吞了|接触|中毒|有毒|危险|不要|禁止|避免|不可|不能|切勿|"
    r"不建议|不应|未使用|没有使用|停止使用|保留.{0,4}(包装|药盒)"
)
_RECOMMENDATION_RE = re.compile(
    r"建议|可以|可用|适合|用于治疗|有效|给予|给药|喂|服用|口服|注射|涂抹|使用"
)


def _context(answer: str, start: int, end: int, radius: int = 32) -> str:
    """只取当前句/当前结构化字段，避免相邻字段的“不要”错误豁免危险建议。"""
    left = max(answer.rfind(delimiter, 0, start) for delimiter in "\n。！？；")
    right_candidates = [
        index
        for delimiter in "\n。！？；"
        if (index := answer.find(delimiter, end)) >= 0
    ]
    sentence_end = min(right_candidates) if right_candidates else len(answer)
    return answer[max(left + 1, start - radius):min(sentence_end, end + radius)]


def _unsafe_dose_context(answer: str, match: re.Match[str]) -> bool:
    context = _context(answer, match.start(), match.end())
    return _SAFE_MENTION_RE.search(context) is None


def _unsafe_frequency_context(answer: str, match: re.Match[str]) -> bool:
    """频率表达只有处于用药语境时才是剂量违规。

    “一天拉三次”“每天观察两次”等症状/护理频率不能误判成用药方案。
    """
    context = _context(answer, match.start(), match.end(), radius=48)
    return (
        _MEDICATION_CONTEXT_RE.search(context) is not None
        and _SAFE_MENTION_RE.search(context) is None
    )


def _unsafe_drug_context(answer: str, match: re.Match[str]) -> bool:
    context = _context(answer, match.start(), match.end())
    if _SAFE_MENTION_RE.search(context):
        return False
    return _RECOMMENDATION_RE.search(context) is not None


class MedicationRules:
    """剂量 + 高风险药物拦截。命中任意一条即违规。"""

    def __init__(self, blocklist_path: Path | None = None):
        self._blocklist_path = blocklist_path or CONFIG_DIR / "medication_blocklist.yaml"
        self._human_drugs: list[str] = []

    def load_sync(self) -> None:
        """同步加载（测试直接调用；async load 包装它）。"""
        with open(self._blocklist_path, encoding="utf-8") as f:
            # 转 str：YAML 会把 "999" 解析成 int，in 检查会炸
            self._human_drugs = [str(d) for d in (yaml.safe_load(f) or [])]

    async def load(self) -> None:
        self.load_sync()

    def violations(self, answer: str) -> list[str]:
        """返回命中的违规描述列表（空 = 通过）。"""
        out: list[str] = []
        if any(_unsafe_dose_context(answer, match) for match in _DOSE_UNIT_RE.finditer(answer)):
            out.append("输出了具体药物剂量")
        if any(
            _unsafe_frequency_context(answer, match)
            for match in _DOSE_FREQ_RE.finditer(answer)
        ):
            out.append("输出了用药频率或疗程")
        if any(_unsafe_drug_context(answer, match) for match in _VET_DRUG_RE.finditer(answer)):
            out.append("输出了具体药物名称")
        for drug in self._human_drugs:
            matches = list(re.finditer(re.escape(drug), answer))
            if any(_unsafe_drug_context(answer, match) for match in matches):
                out.append(f"提到高风险人用药「{drug}」")
                break
        return out
