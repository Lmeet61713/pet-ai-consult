"""病例事实追踪器（FollowUpTracker）—— 从多轮对话中抽取确定性病例事实。

【核心定位】
本模块是"结构化病史采集"组件：把多轮用户自由文本 + 历史/本轮图片视觉观察，
通过确定性词表/正则规则抽取为结构化的 CaseFacts（槽位），供两个下游使用：
    1. CompletenessChecker：判断哪些槽位缺失（missing_slots）→ 生成结构化追问
    2. RiskEngine：基于已确认事实做专科风险升档（如眼部脓性分泌物 → MEDIUM）

【为什么不用模型抽取？】
- 追问与风险升档是安全链路的一部分，必须可解释、可复现、零外部依赖；
- 词表 + 正则在当前覆盖的眼部场景下准确率可控，且天然处理否定语境
  （"眼睛没有红肿" 不会被误判为红肿）。

【第一期覆盖范围】
- 仅眼部（domain="eye"）做完整槽位抽取；其他场景 domain="unknown"，
  但仍会记录已追问过的问题（asked_questions），结构上为后续专科扩展预留。

【眼部槽位（slots）】
- duration：症状持续时间（解析"3天/两周/一个月"等中文数量词）
- discharge_character：分泌物颜色与性状（黄绿/脓性/水样/结痂…）
- eye_discomfort：不适表现（眯眼/抓挠/畏光/疼痛/红肿/睁不开…）
- general_condition：全身状态（精神、食欲正常/异常）
- eye_closeup：是否已有可用的眼部近照（来自图片观察）

【否定语境处理】
所有信号词命中后都会经过 _is_negated() 检查：若信号词前 10 个字符内出现
"没有/没/无/未/否认/不"等否定词，则该信号不计入事实。
"""
from __future__ import annotations

import re

from app.agent.state import ConsultState
from app.core.constants import ImageQuality
from app.schemas.followup import CaseFacts

# 眼部相关解剖/行为词：用于判断图片观察内容是否涉及眼部
_EYE_TERMS = ("眼", "眼睛", "眼球", "眼皮", "眼睑", "眯眼", "眼药", "流泪")

# 用户文字侧的眼部主诉信号词（口语化表达，含"看看眼睛"这类咨询动词）
_USER_EYE_SIGNALS = (
    "眼睛分泌物",
    "眼部分泌物",
    "眼屎",
    "泪痕",
    "流泪",
    "眯眼",
    "抓眼",
    "挠眼",
    "揉眼",
    "畏光",
    "怕光",
    "眼痛",
    "眼睛疼",
    "眼睛红",
    "眼红",
    "眼睛肿",
    "眼部红肿",
    "睁不开",
    "眼球突出",
    "眼球脱出",
    "眼睛出血",
    "眼部出血",
    "眼睛受伤",
    "眼部外伤",
    "突然失明",
    "突然看不见",
    "眼睛怎么",
    "眼睛有问题",
    "看看眼睛",
    "看下眼睛",
    "检查眼睛",
)
# 视觉模型（Vision）观察侧的眼部异常信号词（偏临床/书面表达）
# 与用户侧词表分开维护：模型输出用词更规范，用户用词更口语
_VISION_EYE_ABNORMAL_SIGNALS = (
    "眼睛分泌物",
    "眼部分泌物",
    "眼周分泌物",
    "眼屎",
    "泪痕",
    "流泪",
    "眯眼",
    "抓眼",
    "挠眼",
    "揉眼",
    "畏光",
    "眼痛",
    "眼部疼痛",
    "眼睛红",
    "眼红",
    "眼部红肿",
    "眼睑红肿",
    "眼睑肿胀",
    "结膜充血",
    "角膜浑浊",
    "睁不开",
    "眼球突出",
    "眼球脱出",
    "眼睛出血",
    "眼部出血",
    "眼部外伤",
    "眼睛受伤",
    "突然失明",
    "眼部异常",
)
# 否定词正则：匹配"信号词前面紧跟着否定表达"的语境。
# 形如"眼睛没有红肿""未出现分泌物"——否定词与信号词之间允许 0~5 个非标点字符。
_NEGATION_RE = re.compile(r"(?:没有|没|无|未|否认|并无|不|不会)[^，。；！？]{0,5}$")

# 眼部解剖 + 症状组合正则：兜底捕获"左眼红肿""双眼分泌物增多"等
# "方位词 + 眼/眼睛/眼部… + 症状词"的表达，补充固定词表覆盖不到的句式。
_EYE_ANATOMY_SYMPTOM_RE = re.compile(
    r"(?:左|右|双|两只)?眼(?:睛|部|球|皮|睑|周)?[^，。；！？]{0,8}?"
    r"(?P<symptom>分泌物|眼屎|泪痕|流泪|红肿|发红|肿胀|疼痛|出血|外伤|"
    r"睁不开|突出|脱出|失明|看不见)"
)

# 持续时间正则：捕获阿拉伯数字或中文数字 + 时间单位
# 例："3天"、"两天"、"一个半月"中的"一个月"、"36小时"
_DURATION_RE = re.compile(
    r"(?P<number>\d+(?:\.\d+)?|[一二两三四五六七八九十两半]+)\s*"
    r"(?P<unit>小时|天|周|星期|个月|月)"
)

# 槽位 → 追问问题映射：缺失哪个槽位就问对应问题（CompletenessChecker 调用）
_SLOT_QUESTIONS = {
    "duration": "症状持续多久了，最近是否在加重？",
    "discharge_character": "分泌物是什么颜色，偏水样还是黏稠或脓性？",
    "eye_discomfort": "是否有眯眼、频繁抓挠、畏光或明显疼痛？",
    "general_condition": "宠物目前的精神和食欲是否正常？",
    "eye_closeup": "可以补充患眼正面近照和双眼对比照吗？",
}


class FollowUpTracker:
    """多轮病例事实抽取器（第一期覆盖眼部场景，结构保留后续专科扩展空间）。

    【使用方式】
        tracker = FollowUpTracker()
        facts = tracker.extract(state)   # 抽取结构化事实，写入 state.case_facts
        questions = tracker.questions(facts)  # 基于缺失槽位生成追问

    【输出 CaseFacts 的关键字段】
    - domain：场景域（"eye" / "unknown"）
    - answered_slots / missing_slots：已答/缺失槽位
    - confirmed_facts：人类可读的已确认事实列表（进入模型上下文）
    - asked_questions：历史已问过的问题（避免重复追问）
    """

    def extract(self, state: ConsultState) -> CaseFacts:
        """从状态总线中聚合多轮文本与图片观察，抽取病例事实。

        【处理流程】
        1. 聚合用户文本：历史所有轮次 user_text + 本轮 text，用"。"连接
        2. 聚合视觉观察：历史轮次 image_findings + 本轮 vision_findings 的
           observations 与 red_flags
        3. 场景判定：用户文本命中眼部信号，或视觉观察命中眼部异常信号
           → domain="eye"，否则 domain="unknown"（仍记录 asked_questions）
        4. 眼部槽位抽取：持续时间、分泌物颜色/性状、眼部不适体征、
           精神/食欲状态、是否有可用眼部近照
        5. 槽位核对：已答槽位 → answered_slots，未答 → missing_slots
        6. 生成 confirmed_facts：供模型上下文使用的简短中文事实句

        【否定语境】
        所有词表命中都经过否定检查（"没有红肿"不计入红肿）。

        :param state: 状态总线（读取 history/text/vision_findings）
        :return: 结构化病例事实 CaseFacts
        """
        # 1. 聚合多轮用户文本（跳过空文本）
        user_texts = [turn.user_text for turn in state.history if turn.user_text.strip()]
        if state.text.strip():
            user_texts.append(state.text.strip())
        combined = "。".join(user_texts)

        # 2. 聚合历史轮次与本轮的所有图片视觉观察
        historical_findings = [
            finding
            for turn in state.history
            for finding in turn.image_findings
        ]
        findings = [*historical_findings, *state.vision_findings]
        # 视觉文本 = 所有观察描述 + 红旗信号，用"。"连接
        vision_text = "。".join(
            item
            for finding in findings
            for item in [*finding.observations, *finding.red_flags]
            if item
        )
        # 3. 场景域判定：用户文字或视觉观察任一命中眼部信号即视为眼部病例
        domain = (
            "eye"
            if _has_eye_complaint(combined, _USER_EYE_SIGNALS)
            or _has_eye_complaint(vision_text, _VISION_EYE_ABNORMAL_SIGNALS)
            else "unknown"
        )

        # 已追问过的问题去重汇总（跨轮次），用于避免重复追问
        asked_questions = _dedup(
            question
            for turn in state.history
            for question in turn.follow_up_questions
        )
        # 非眼部场景：只返回 domain + 已问问题，不做槽位抽取
        if domain != "eye":
            return CaseFacts(domain=domain, asked_questions=asked_questions)

        # 4. 眼部槽位抽取 --------------------------------------------------
        # 4.1 持续时间（原文 + 换算为小时数，供风险引擎判断 >48h）
        duration, duration_hours = _extract_duration(combined)
        # 4.2 分泌物颜色：取文本中最后出现（即最近一轮描述）的颜色词
        discharge_color = _last_term(
            combined,
            ("黄绿色", "黄绿", "黄色", "绿色", "白色", "透明", "褐色", "红色", "血性"),
        )
        # 4.3 分泌物性状：黏稠/脓性/水样/结痂（支持别名）
        characters = _present_terms(
            combined,
            {
                "黏稠": ("黏稠", "粘稠", "比较黏", "很黏"),
                "脓性": ("脓性", "像脓", "脓样"),
                "水样": ("水样", "清水样", "很稀"),
                "结痂": ("结痂",),
            },
        )
        # 4.4 眼部不适体征：同时从用户文字和视觉观察中抽取
        eye_signs = _present_terms(
            combined + "。" + vision_text,
            {
                "眯眼": ("眯眼",),
                "抓挠": ("抓挠", "抓眼", "挠眼", "揉眼"),
                "畏光": ("畏光", "怕光"),
                "疼痛": ("疼痛", "明显疼", "眼痛"),
                "红肿": ("红肿", "眼红", "发红"),
                "睁不开": ("睁不开", "无法睁眼"),
                "眼球突出": ("眼球突出", "眼球脱出"),
                "突然失明": ("突然失明", "突然看不见"),
                "眼部出血": ("眼球出血", "眼睛出血", "眼部出血"),
                "眼部外伤": ("眼部外伤", "眼睛受伤", "眼球受伤"),
            },
        )
        # 4.5 全身状态：精神、食欲分别判定正常/异常（取最后一次有效描述）
        energy_status = _extract_status(
            combined,
            subject="精神",
            normal_terms=("精神和食欲正常", "精神、食欲正常", "精神食欲正常", "精神正常", "精神很好"),
            abnormal_terms=("没精神", "精神不好", "精神差", "精神萎靡", "精神沉郁"),
        )
        appetite_status = _extract_status(
            combined,
            subject="食欲",
            normal_terms=("精神和食欲正常", "精神、食欲正常", "精神食欲正常", "食欲正常", "吃饭正常"),
            abnormal_terms=("没食欲", "食欲不好", "食欲差", "不吃东西", "拒食"),
        )
        # 4.6 眼部近照：任一可用（非 unusable）图片的部位/观察中提到眼 → 视为已有近照
        eye_closeup = any(
            finding.image_quality != ImageQuality.UNUSABLE
            and _contains_any(
                "。".join([*finding.body_parts, *finding.observations]),
                _EYE_TERMS,
            )
            for finding in findings
        )

        # 5. 槽位核对：哪些槽位已有答案 -------------------------------------
        answered: list[str] = []
        if duration:
            answered.append("duration")
        if discharge_color or characters:
            answered.append("discharge_character")
        if eye_signs:
            answered.append("eye_discomfort")
        # 精神和食欲都明确时才算全身状态槽位已答
        if energy_status and appetite_status:
            answered.append("general_condition")
        if eye_closeup:
            answered.append("eye_closeup")

        # 固定槽位顺序（决定追问顺序）：时长 → 分泌物 → 不适 → 全身 → 近照
        slots = [
            "duration",
            "discharge_character",
            "eye_discomfort",
            "general_condition",
            "eye_closeup",
        ]
        missing = [slot for slot in slots if slot not in answered]

        # 6. 组装人类可读的已确认事实句（进入模型上下文，帮助生成针对性回答）
        facts = []
        if duration:
            facts.append(f"症状持续{duration}")
        if discharge_color or characters:
            value = "、".join(p for p in (discharge_color, "、".join(characters)) if p)
            facts.append(f"分泌物为{value}")
        if eye_signs:
            facts.append("伴随" + "、".join(eye_signs))
        if energy_status:
            facts.append(f"精神{_status_label(energy_status)}")
        if appetite_status:
            facts.append(f"食欲{_status_label(appetite_status)}")
        if eye_closeup:
            facts.append("已有可用眼部图片")

        return CaseFacts(
            domain=domain,
            duration=duration,
            duration_hours=duration_hours,
            discharge_color=discharge_color,
            discharge_character="、".join(characters),
            eye_signs=eye_signs,
            energy_status=energy_status,
            appetite_status=appetite_status,
            eye_closeup_available=eye_closeup,
            answered_slots=answered,
            missing_slots=missing,
            asked_questions=asked_questions,
            confirmed_facts=facts,
        )

    @staticmethod
    def questions(facts: CaseFacts, *, limit: int = 3) -> list[str]:
        """根据缺失槽位生成结构化追问问题列表。

        按槽位固定顺序（时长 → 分泌物 → 不适 → 全身 → 近照）映射到
        _SLOT_QUESTIONS 中的中文追问，最多返回 limit 个，避免一次追问过多。

        :param facts: extract() 产出的病例事实
        :param limit: 单次最多追问数量（默认 3）
        :return: 追问问题文本列表
        """
        return [_SLOT_QUESTIONS[slot] for slot in facts.missing_slots if slot in _SLOT_QUESTIONS][
            :limit
        ]


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    """文本中是否包含 terms 中的任意一个词（不做否定检查）。"""
    return any(term in text for term in terms)


def _has_eye_complaint(text: str, terms: tuple[str, ...]) -> bool:
    """判断文本中是否存在（非否定的）眼部主诉/异常信号。

    两路判定，任一命中即返回 True：
    1. 固定词表 terms 中任一词出现且不在否定语境中；
    2. 解剖+症状正则 _EYE_ANATOMY_SYMPTOM_RE 命中且症状词不在否定语境中。

    :param text: 待判定文本（用户文字或视觉观察拼接）
    :param terms: 信号词表（_USER_EYE_SIGNALS 或 _VISION_EYE_ABNORMAL_SIGNALS）
    :return: True 表示存在眼部异常描述
    """
    # 第一路：逐词扫描所有出现位置，跳过被否定的命中
    for term in terms:
        start = 0
        while (index := text.find(term, start)) >= 0:
            if not _is_negated(text, index):
                return True
            start = index + len(term)
    # 第二路：正则兜底（如"左眼发红""双眼分泌物"）
    return any(
        not _is_negated(text, match.start("symptom"))
        for match in _EYE_ANATOMY_SYMPTOM_RE.finditer(text)
    )


def _is_negated(text: str, index: int) -> bool:
    """判断 index 位置前的上下文是否为否定表达。

    取信号词前 10 个字符，用 _NEGATION_RE 检查是否以
    "没有/没/无/未/否认/不"等否定词引导。

    :param text: 完整文本
    :param index: 信号词出现的位置
    :return: True 表示该信号被否定（不应计为事实）
    """
    return _NEGATION_RE.search(text[max(0, index - 10):index]) is not None


def _last_term(text: str, terms: tuple[str, ...]) -> str:
    """返回文本中"最后出现"（位置最靠后）且未被否定的词。

    多轮对话中用户可能反复描述同一症状，取最后一次描述代表最新状态
    （如先说"白色分泌物"后说"黄绿色分泌物"，以黄绿色为准）。

    :param text: 聚合文本
    :param terms: 候选词（如颜色词表）
    :return: 命中的词；无命中返回空字符串
    """
    matches = [
        (text.rfind(term) + len(term), len(term), term)
        for term in terms
        if text.rfind(term) >= 0 and not _is_negated(text, text.rfind(term))
    ]
    # max 按"词结束位置"比较，位置最大即最后出现；并列时按词长、词本身兜底排序
    return max(matches)[2] if matches else ""


def _present_terms(text: str, aliases: dict[str, tuple[str, ...]]) -> list[str]:
    """扫描别名词表，返回文本中实际出现（且未被否定）的标准标签列表。

    :param text: 待扫描文本
    :param aliases: 标准标签 → 别名元组 的映射
                     （如 {"脓性": ("脓性", "像脓", "脓样")}）
    :return: 按首次出现位置排序的标准标签列表（如 ["红肿", "抓挠"]）
    """
    found: list[tuple[int, str]] = []
    for label, terms in aliases.items():
        positions: list[int] = []
        for term in terms:
            start = 0
            while (index := text.find(term, start)) >= 0:
                if not _is_negated(text, index):
                    positions.append(index)
                start = index + len(term)
        if positions:
            # 同一标签取最早出现位置，用于最终按出现顺序排序
            found.append((min(positions), label))
    return [label for _, label in sorted(found)]


def _extract_duration(text: str) -> tuple[str, float | None]:
    """从文本中提取持续时间，取最后一次出现的时间表达。

    :return: (原文表达, 换算后的小时数)；无命中返回 ("", None)。
             例："两天" → ("两天", 48.0)；"一个月" → ("一个月", 720.0)
    """
    matches = list(_DURATION_RE.finditer(text))
    if not matches:
        return "", None
    # 取最后一次时间描述（多轮对话中代表最新/最准确的病程）
    match = matches[-1]
    raw_number = match.group("number")
    number = _parse_number(raw_number)
    unit = match.group("unit")
    # 各时间单位换算为小时的倍率
    multiplier = {
        "小时": 1,
        "天": 24,
        "周": 24 * 7,
        "星期": 24 * 7,
        "个月": 24 * 30,
        "月": 24 * 30,
    }[unit]
    return match.group(0), number * multiplier if number is not None else None


def _parse_number(value: str) -> float | None:
    """把阿拉伯数字或中文数字（含"半/十/两"）解析为 float。

    支持："3" → 3.0；"半" → 0.5；"十" → 10；"二十三" → 23；"两" → 2。
    无法解析时返回 None。
    """
    try:
        return float(value)
    except ValueError:
        pass
    if value == "半":
        return 0.5
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        # "二十三" → 2*10 + 3；"十" 开头省略十位数字时按 1*10 处理
        left, right = value.split("十", 1)
        return digits.get(left, 1) * 10 + digits.get(right, 0)
    return float(digits[value]) if value in digits else None


def _extract_status(
    text: str,
    *,
    subject: str,
    normal_terms: tuple[str, ...],
    abnormal_terms: tuple[str, ...],
) -> str:
    """提取"精神/食欲"类二元状态：normal（正常）/ abnormal（异常）/ ""（未提及）。

    取文本中最后出现的有效状态描述（位置最靠后优先），模拟多轮对话中
    "最新描述覆盖旧描述"的语义。若只提到主体词（如"精神"）但无任何
    正常/异常描述，返回空字符串表示未知。

    :param subject: 主体词（"精神" / "食欲"），用于判断是否提及该维度
    :param normal_terms: 正常表达词表
    :param abnormal_terms: 异常表达词表
    :return: "normal" / "abnormal" / ""
    """
    occurrences: list[tuple[int, str]] = []
    for status, terms in (("normal", normal_terms), ("abnormal", abnormal_terms)):
        for term in terms:
            start = 0
            while (index := text.find(term, start)) >= 0:
                if not _is_negated(text, index):
                    occurrences.append((index, status))
                start = index + len(term)
    if not occurrences and subject in text:
        # 提到了主体但没有有效状态词：状态未知
        return ""
    return max(occurrences)[1] if occurrences else ""


def _status_label(status: str) -> str:
    """把内部状态码转为中文标签（normal→正常，其余→异常）。"""
    return "正常" if status == "normal" else "异常"


def _dedup(items) -> list[str]:
    """按出现顺序去重并过滤空值（dict.fromkeys 保序去重）。"""
    return list(dict.fromkeys(item for item in items if item))
