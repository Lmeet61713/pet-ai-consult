"""过度诊断表达规则（v5 §13.3：确诊断言 / 过度保证 / 矛盾建议）"""
from __future__ import annotations

import re

# 确诊断言（"可能是/疑似/常见原因"不在此列）
# "确诊"单独处理（带语境豁免）；其余为强断言词直接拦截
_DIAGNOSIS_RE = re.compile(r"可以确定是|肯定就是|确定是.{0,6}病|诊断为|就是得了|一定是.{0,4}病|确定.{0,2}就是")
_DIAGNOSIS_CONFIRM_RE = re.compile(r"确诊")

# "确诊"非断言语境：后面 0-6 字符内出现"需/要/应/必须/通过/检查/后才能"等
# （"确诊哮喘需要做检查"是途径不是断言）；条件式（如果/若/疑似/可能/怀疑）前导也豁免
_CONFIRM_NON_ASSERT_SUFFIX_RE = re.compile(
    r"确诊(?=[^，。；！？\n]{0,10}(需|要|应|必须|通过|靠|检查|检测|影像|血检|活检|听诊|后|之后|之前|前|的|和|或|与|在|时|看|问|了解|确认))"
)
_CONFIRM_CONDITIONAL_PREFIX_RE = re.compile(
    r"(?:如果|如|若|一旦|疑似|可能|怀疑|未|没有|还没|需|要|无法|不能|"
    r"兽医|医生|就医|检查|才能|方可|进一步)[^，。；！？\n]{0,12}确诊"
)

# 否定语境（"不要自行确诊""切勿自行诊断"）与条件语境（"确诊后…应遵循兽医建议"）不误判
_DIAGNOSIS_NEGATION_RE = re.compile(
    r"(?:不要|避免|禁止|切勿|不可|不应|不建议|不能|请勿|不得)[^，。；！？\n]{0,12}$"
)
_DIAGNOSIS_CONDITIONAL = ("确诊后", "自行确诊", "由兽医确诊", "经兽医确诊", "确诊需")

# 过度保证（"可以不用就医"类）
_OVERASSURANCE_RE = re.compile(
    r"不用去医院|不需要就医|不用就医|不必就医|没必要去医院|放心没事|没有大碍|"
    r"不用管|自己会好|不用看|肯定没事|一定没事|绝对没事|不需要看医生"
)

# 矛盾处置（第一版：冷敷/热敷、禁食/喂食 组合）
_CONTRADICTIONS = (
    (("热敷", "热敷一下"), ("冷敷", "冰敷")),
    (("禁食", "不要喂食", "停止喂食"), ("多喂", "继续喂食", "正常喂食")),
)

_ADVICE_NEGATION_RE = re.compile(r"(?:不要|避免|禁止|切勿|不可|不应|不建议|不能|请勿)[^，。；！？\n]{0,12}$")
_FEEDING_SEQUENCE_TERMS = (
    "如果", "若", "待", "之后", "以后", "停止呕吐后", "不再呕吐后", "恢复后", "少量",
)

# 免责声明关键词（回答必须包含，v5 §13.3 最后一条）
_DISCLAIMER_RE = re.compile(r"不能替代.{0,8}(兽医|医生|检查)|仅供参考|执业兽医|无法替代专业")

# 就医提及（遗漏已命中急症检查用）
_GO_VET_RE = re.compile(r"就医|医院|兽医|急诊")

# 把可能性写成确定事实（"这就是/肯定是"轻量版）
_CERTAINTY_RE = re.compile(r"就是因为|就是由于|可以肯定")

_SOFTEN_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"可以确定是|肯定就是|确定就是"), "目前可能是"),
    (
        re.compile(r"(?:已经|已)?(?:被|可)?(?:明确)?(?:诊断为|确诊为)|就是得了"),
        "目前可能存在",
    ),
    (re.compile(r"一定是([^，。；！？\n]{0,8})病"), r"可能与\1相关"),
    (re.compile(r"确定是([^，。；！？\n]{0,8})病"), r"可能与\1相关"),
    (
        re.compile(r"(?:这)?就是(?:因为|由于)([^，。；！？\n]+)"),
        r"这可能与\1有关",
    ),
    (re.compile(r"可以肯定"), "从目前信息还不能确定"),
    (re.compile(r"(?:已经|已)确诊(?:为|是)?"), "目前可能存在"),
    (re.compile(r"肯定没事|一定没事|绝对没事|放心没事"), "目前未见明确急症信号，但仍需继续观察"),
    (re.compile(r"不用去医院|不需要就医|不用就医|不必就医|没必要去医院"), "目前可先观察；若持续或加重仍需就医"),
)


def soften_diagnosis_assertions(text: str) -> str:
    """把明确的确诊/确定语气改为条件化表达，不改变护理和就医信息。"""
    softened = text
    for pattern, replacement in _SOFTEN_REPLACEMENTS:
        softened = pattern.sub(replacement, softened)
    return softened


def _affirmative_segments(answer: str, terms: tuple[str, ...]) -> list[str]:
    """返回肯定式建议所在句，忽略“不要/避免”等禁止表达。"""
    hits: list[str] = []
    for segment in re.split(r"[，。；！？\n]", answer):
        for term in terms:
            start = segment.find(term)
            if start < 0:
                continue
            if _ADVICE_NEGATION_RE.search(segment[:start]):
                continue
            hits.append(segment)
            break
    return hits


def _has_conflicting_advice(answer: str) -> bool:
    hot = _affirmative_segments(answer, _CONTRADICTIONS[0][0])
    cold = _affirmative_segments(answer, _CONTRADICTIONS[0][1])
    if hot and cold:
        return True

    fasting = _affirmative_segments(answer, _CONTRADICTIONS[1][0])
    feeding = _affirmative_segments(answer, _CONTRADICTIONS[1][1])
    if not fasting or not feeding:
        return False
    # “暂时停食，停止呕吐后少量恢复”是分阶段建议，不属于自相矛盾。
    return any(
        not any(marker in segment for marker in _FEEDING_SEQUENCE_TERMS)
        for segment in feeding
    )


def _has_diagnosis_assertion(answer: str) -> bool:
    """逐句检查确诊断言。

    豁免：
    - 否定语境（不要自行确诊/切勿自行诊断）；
    - 条件语境（确诊后…应遵循兽医建议）；
    - 途径语境（确诊哮喘需要做检查）——"确诊"后跟非完成式动词/名词；
    - 条件式前导（如果/若/疑似/可能/怀疑…确诊）。
    """
    for segment in re.split(r"[，。；！？\n]", answer):
        seg = segment.strip()
        if not seg:
            continue
        for match in _DIAGNOSIS_RE.finditer(seg):
            prefix = seg[: match.start()]
            if _DIAGNOSIS_NEGATION_RE.search(prefix):
                continue
            if any(term in seg for term in _DIAGNOSIS_CONDITIONAL):
                continue
            return True
        for match in _DIAGNOSIS_CONFIRM_RE.finditer(seg):
            prefix = seg[: match.start()]
            if _DIAGNOSIS_NEGATION_RE.search(prefix):
                continue
            if any(term in seg for term in _DIAGNOSIS_CONDITIONAL):
                continue
            # 名词化/词组误伤豁免："明确诊断/鉴别诊断/的诊断"等含"确诊"子串但非断言
            if re.search(r"明确诊断|鉴别诊断|的诊断|诊断需|诊断需要|检查明确|以明确|进行诊断|做出诊断|做诊断", seg):
                continue
            if _CONFIRM_CONDITIONAL_PREFIX_RE.search(seg):
                continue
            if _CONFIRM_NON_ASSERT_SUFFIX_RE.search(seg):
                continue
            return True
    return False


class DiagnosisRules:
    @staticmethod
    def violations(answer: str) -> list[str]:
        out: list[str] = []
        if _has_diagnosis_assertion(answer):
            out.append("出现确诊式断言")
        if _OVERASSURANCE_RE.search(answer):
            out.append("给出'可以不用就医'类过度保证")
        if _CERTAINTY_RE.search(answer):
            out.append("把可能性写成确定事实")
        if _has_conflicting_advice(answer):
            out.append("处置建议互相矛盾")
        if not _DISCLAIMER_RE.search(answer):
            out.append("缺少免责声明")
        return out

    @staticmethod
    def mentions_vet(answer: str) -> bool:
        return _GO_VET_RE.search(answer) is not None
