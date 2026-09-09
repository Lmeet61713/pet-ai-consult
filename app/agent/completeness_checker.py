"""信息完整度判断器（CompletenessChecker，v5 §9.3 COMPLETENESS_CHECK）。

【核心定位】
在问诊流水线 COMPLETENESS_AND_RISK 阶段运行，决定"本轮信息是否足以生成回答"：
- 信息充足（need_more_info=False）→ NORMAL_GENERATION（正常生成）
- 信息不足（need_more_info=True） → PROVISIONAL_GENERATION（先给简短初步建议，
  再附带关键追问），或直接返回固定追问模板（多宠歧义/无图无文等硬缺失）

【回答优先原则】
信息不足不是拒绝回答的理由：只有"完全没有输入"才硬性索要材料；
短症状描述走 provisional 模式——先初步回应，再追问最多 2 个真正影响
风险分级的问题，避免一问一答的机械体验。

【判定判据（按 evaluate() 中的优先级顺序）】
1. 多宠歧义：多只宠物且无法确定问的是哪只 → pet_ambiguous 固定追问
2. 无图无文无历史 → hard_need，请上传照片或文字描述
3. 图片 unusable（模糊/遮挡/非宠物）→ hard_need，请重拍
4. 明确眼病且有缺失槽位 → hard_need，结构化眼部追问（FollowUpTracker）
5. Vision 建议补图（needs_more_images）→ hard_need，请补充角度
6. 有图但全程无文字 → hard_need，追问症状时长/精神食欲 + 图片建议问题
7. 日常护理频率问题（洗澡/刷牙周期等）→ 信息完整，无需追问
8. 短文本信息覆盖薄 → keyword_thin，provisional 模式 + ≤2 个关键追问
9. 其余情况 → 信息充足，正常生成

【与 RAG 的协作】
命中知识卡片时，卡片的 questions_to_ask 会作为 rag_questions 传入；
短症状场景优先使用按症状定制的追问，卡片问题作为兜底。
"""
from __future__ import annotations

import logging
import re

from app.agent.followup_tracker import FollowUpTracker
from app.agent.state import ConsultState
from app.schemas.safety import CompletenessResult

logger = logging.getLogger(__name__)

# 关键信息类别指示词：文本中出现即认为用户已提供该类信息。
# 用于"命中卡片但信息太薄"时判断信息覆盖度（v1.2 §4.7 查缺）：
# 覆盖 ≥2 个类别即视为信息足够；不足 2 类且总字数 <80 才触发追问。
_KEY_INFO_SIGNALS: dict[str, tuple[str, ...]] = {
    "duration": ("天", "小时", "周", "持续", "多久", "昨天", "今天"),
    "general": ("精神", "食欲", "活力", "蔫", "吃", "喝水", "排便"),
    "vomit": ("吐", "呕"),
    "blood": ("血", "便血", "黑便"),
    "frequency": ("几次", "几回", "频繁", "次/天", "多次"),
    "medication": ("用药", "疫苗", "驱虫", "药"),
    "pain": ("疼", "痛", "惨叫", "呻吟", "抓", "挠"),
}

# 呼吸道症状词：短症状追问时用于匹配"打喷嚏/咳嗽/流鼻涕"类主诉
_RESPIRATORY_TERMS = (
    "打喷嚏",
    "喷嚏",
    "咳嗽",
    "流鼻涕",
    "鼻塞",
    "呼吸",
    "喘",
)

# 精神沉郁类非特异性症状词：匹配"没精神/蔫/状态不好"类主诉
_LOW_ENERGY_TERMS = (
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
)

# 腹泻类症状词：匹配"腹泻/拉稀/水样便"类主诉
_DIARRHEA_TERMS = (
    "腹泻",
    "拉稀",
    "稀便",
    "水样便",
)

# 猫下巴（痤疮/黑下巴）皮肤病特征词：知识库暂无对应卡片，
# 命中时走定制追问，避免错误匹配到其他皮肤卡片
_CAT_CHIN_SKIN_TERMS = (
    "下巴黑",
    "下巴有黑",
    "下巴颗粒",
    "下巴结痂",
    "颏部黑",
    "猫痤疮",
    "黑下巴",
)

# 歧义排泄词："上厕所/如厕"在口语中可能指排尿也可能指排便，
# 未出现明确排泄词时需要追问澄清，防止检索器误匹配
_AMBIGUOUS_ELIMINATION_TERMS = ("上厕所", "去厕所", "跑厕所", "如厕")
# 明确排泄词：出现任一则说明用户已说清是泌尿还是肠道问题
_EXPLICIT_ELIMINATION_TERMS = (
    "排尿", "小便", "撒尿", "尿尿", "尿频", "尿血", "尿不出", "尿量", "尿液",
    "排便", "大便", "便便", "拉屎", "粪便", "拉稀", "腹泻", "便秘", "软便", "便血", "黑便",
)

# 日常护理频率问题（多久洗一次澡/刷牙/剪指甲等）本身已经完整，
# 不需要追问“持续多久/精神食欲”，命中即判定信息充足。
_GENERAL_CARE_PATTERNS = (
    re.compile(r"(?:多久|多长时间|几天|几周).{0,8}(?:洗一次澡|洗澡)"),
    re.compile(r"(?:洗澡).{0,8}(?:频率|间隔|多久一次|多长时间一次)"),
    re.compile(
        r"(?:多久|多长时间|几天|几周).{0,8}"
        r"(?:刷一次牙|刷牙|剪一次指甲|剪指甲|梳一次毛|梳毛)"
    ),
)


class CompletenessChecker:
    """信息完整度判断器：决定本轮能否直接回答，还是需要追问/补材料。

    【输出 CompletenessResult】
    - need_more_info=True + reason="hard_need"：硬缺失，必须先索要材料/关键信息
      （无图无文、图片不可用、眼病槽位缺失、需要补图、纯图无文字）
    - need_more_info=True + reason="pet_ambiguous"：多宠歧义，固定追问"问的是哪只"
    - need_more_info=True + reason="keyword_thin"：信息偏薄，走 provisional 模式
      （先初步回答，再带 ≤2 个关键追问）
    - need_more_info=False + reason="general_care"：日常护理问题，信息完整
    - need_more_info=False：其余信息充足场景，正常生成

    【副作用】
    evaluate() 会调用 FollowUpTracker.extract(state) 并把结果写入
    state.case_facts，供 RiskEngine 与生成阶段使用。
    """

    def __init__(self, tracker: FollowUpTracker | None = None):
        """初始化完整度检查器。

        :param tracker: 病例事实追踪器；不传时默认新建 FollowUpTracker 实例
        """
        self.tracker = tracker or FollowUpTracker()

    def evaluate(
        self,
        state: ConsultState,
        *,
        rag_questions: list[str] | None = None,
    ) -> CompletenessResult:
        """信息完整度判断（状态机 COMPLETENESS_AND_RISK 阶段调用）。

        【判据优先级】（从上到下，命中即返回）
        1. 多宠歧义 → pet_ambiguous
        2. 无图无文无历史 → hard_need（请上传/描述）
        3. 存在 unusable 图片 → hard_need（请重拍）
        4. 眼部病例有缺失槽位 → hard_need（结构化眼部追问）
        5. Vision 建议补图 → hard_need（请补角度）
        6. 有图但无任何文字 → hard_need（追问症状 + 图片建议问题）
        7. 日常护理频率问题 → 信息充足（general_care）
        8. 短文本信息覆盖薄 → keyword_thin（provisional + 定制追问）
        9. 其余 → 信息充足

        :param state: 状态总线（读取 text/history/image_inputs/vision_findings/pets；
                      写入 case_facts）
        :param rag_questions: 命中知识卡片的 questions_to_ask（可为空）。
                              用户文字很短且未覆盖关键信息类别时，用卡片问题
                              触发追问，而不是直接长篇回答。
        :return: CompletenessResult（need_more_info + reason + questions）
        """
        text = state.text.strip()
        # 历史轮次用户文本拼接，用于多轮场景的信息覆盖度判断
        history_text = " ".join(t.user_text or "" for t in state.history)
        findings = state.vision_findings
        # 副作用：抽取结构化病例事实写入 state（眼部槽位、已问问题等）
        state.case_facts = self.tracker.extract(state)

        # 判据 1：多宠歧义（2026-08-19）：多只宠物且无法确定"问的是哪只" → 追问确认
        if state.pets and len(state.pets) > 1 and not self._pet_resolved(state, text):
            names = " / ".join(
                p.display_name for p in state.pets if p.name
            ) or f"{len(state.pets)} 只宠物"
            return CompletenessResult(
                need_more_info=True,
                reason="pet_ambiguous",
                questions=[f"您问的是哪一只呢？（{names}）"],
            )

        # 判据 2：完全没有输入（无图片、无本轮文字、无历史）→ 请用户提供材料
        if not state.image_inputs and not text and not history_text:
            return CompletenessResult(
                need_more_info=True,
                reason="hard_need",
                questions=["请上传宠物照片，或描述一下目前的情况。"],
            )

        # 判据 3：存在不可用图片（模糊/遮挡/非宠物照片）→ 请重拍
        if findings:
            if any(f.image_quality == "unusable" for f in findings):
                return CompletenessResult(
                    need_more_info=True,
                    reason="hard_need",
                    questions=["图片无法使用（模糊/遮挡/不是宠物照片），请重新拍摄清晰照片。"],
                )

        # 判据 4：明确眼部异常时，结构化病史比一般性的补拍建议更有诊断价值。
        # 不可用图片仍在上方优先处理；可用图片即使建议补角度，也先问缺失槽位。
        if state.case_facts.domain == "eye" and state.case_facts.missing_slots:
            return CompletenessResult(
                need_more_info=True,
                reason="hard_need",
                questions=self.tracker.questions(state.case_facts),
            )

        # 判据 5：视觉模型认为一张照片不够（角度/距离不足）→ 请补充其他角度
        if findings:
            if any(f.needs_more_images for f in findings):
                return CompletenessResult(
                    need_more_info=True,
                    reason="hard_need",
                    questions=["一张照片可能不够，请补充其他角度的照片。"],
                )

        # 判据 6：有图片但全程无文字描述 → 模型没有可依据的症状事实，
        # 追问最基础的时长/精神食欲，并附上视觉模型建议的问题
        if not text and not history_text:
            questions = ["症状持续多久了？", "精神、食欲、排便情况如何？"]
            for f in findings:
                questions.extend(f.suggested_questions)
            return CompletenessResult(
                need_more_info=True, reason="hard_need", questions=questions
            )

        # 判据 7：日常护理频率问题（多久洗一次澡等）本身信息完整，无需追问
        if self._is_general_care_question(text):
            return CompletenessResult(need_more_info=False, reason="general_care")

        # 判据 8：追问查缺——短文本 + 关键信息覆盖少 → 先给简短初步建议，
        # 再追问最多两个真正影响风险判断的问题（provisional 模式）。
        if self._information_too_thin(text, history_text):
            return CompletenessResult(
                need_more_info=True,
                reason="keyword_thin",
                questions=self._thin_questions(
                    state,
                    text=f"{text} {history_text}".strip(),
                    rag_questions=rag_questions or [],
                ),
            )

        # 判据 9：信息充足，正常生成
        return CompletenessResult(need_more_info=False)

    @staticmethod
    def _information_too_thin(text: str, history_text: str) -> bool:
        """关键信息覆盖不足 → 应追问。

        不能用多轮文字拼接后的字符数直接判定完整；用户连续说“状态不好”
        “看起来没精神”虽然总长度可能超过 20，实际上仍只提供了一个信息类别。
        超过 80 字的自然描述才视为可能包含了词表未覆盖的有效细节。
        """
        combined = f"{text} {history_text}".strip()
        covered = sum(
            1 for terms in _KEY_INFO_SIGNALS.values() if any(t in combined for t in terms)
        )
        if covered >= 2:
            return False
        return len(combined) < 80

    @staticmethod
    def _thin_questions(
        state: ConsultState,
        *,
        text: str,
        rag_questions: list[str],
    ) -> list[str]:
        """为短症状生成"少而关键"的追问（最多 2 个），避免照搬卡片三条问题。

        【按症状定制的追问分支】
        - 歧义排泄（只说"上厕所"）→ 先澄清小便还是大便，再问红旗症状
        - 腹泻 → 持续时间/次数/便血 + 精神食欲/呕吐腹痛
        - 猫黑下巴 → 颗粒能否擦掉/有无红肿渗液 + 抓挠/食盆材质
        - 呼吸道症状 → 喷嚏时长频率 + 鼻涕眼分泌物/呼吸费力
        - 精神沉郁 → 持续时间/反应力 + 食欲饮水/呕吐腹泻疼痛
        - 其余 → 优先用 RAG 卡片问题；卡片也没有则用通用时长 + 全身状态追问

        【兜底规则】
        - 物种未知时，最前面插入"猫咪还是狗狗"的确认问题
        - 过滤掉历史已问过的问题（asked_questions）
        - 若过滤后为空（用户上一轮没回答关键问题），改用更聚焦的
          重新确认措辞，保证 provisional 响应始终带有追问
        """
        species_known = bool(
            state.pet_info and state.pet_info.species in ("cat", "dog")
        )
        species_question = "想先确认一下，您说的是猫咪还是狗狗呢？"

        # 分支 1：只说"上厕所/去厕所"且未明确小便/大便 → 先澄清排泄类型
        if (
            any(term in text for term in _AMBIGUOUS_ELIMINATION_TERMS)
            and not any(term in text for term in _EXPLICIT_ELIMINATION_TERMS)
        ):
            questions = [
                "想先确认一下，您说的经常上厕所，是频繁小便还是频繁大便呢？每次的量有没有变化？",
                "另外，有没有尿血、用力却尿不出、腹泻、呕吐，或者精神食欲明显变差？",
            ]
        # 分支 2：腹泻 → 时长/次数/血便 + 全身状态
        elif any(term in text for term in _DIARRHEA_TERMS):
            subject = state.pet_info.display_name if state.pet_info else "宠物"
            questions = [
                "想先确认一下：腹泻持续多久了，一天大概几次，粪便里有没有鲜血或黑色柏油样内容呢？",
                f"另外，{subject}目前精神和食欲怎么样，有没有呕吐、腹痛或明显不愿喝水呢？",
            ]
        # 分支 3：猫黑下巴（痤疮）→ 皮损细节 + 抓挠/食具（塑料食盆是常见诱因）
        elif any(term in text for term in _CAT_CHIN_SKIN_TERMS):
            questions = [
                "想先确认一下：下巴的黑色颗粒能否轻轻擦掉，下面有没有发红、肿胀、渗液或异味呢？",
                "另外，猫咪会不会频繁抓挠或蹭下巴，食盆和水盆是什么材质、多久清洗一次呢？",
            ]
        # 分支 4：呼吸道症状 → 喷嚏时长频率 + 鼻涕眼部分泌物/张口呼吸
        elif any(term in text for term in _RESPIRATORY_TERMS):
            questions = [
                "想先确认一下：打喷嚏持续多久了，一天大概有几次呢？",
                "另外，有没有流鼻涕、眼分泌物，或者张口呼吸、呼吸费力呢？",
            ]
        # 分支 5：精神沉郁 → 持续时间/反应力/行动能力 + 食欲饮水和红旗症状
        elif any(term in text for term in _LOW_ENERGY_TERMS):
            questions = [
                "想先确认一下：没精神持续多久了，叫它时有反应、还能正常站立和走动吗？",
                "另外，食欲和饮水有没有明显下降，有没有呕吐、腹泻、呼吸费力或明显疼痛呢？",
            ]
        # 分支 6：兜底——优先用知识卡片自带问题，再不行用通用追问
        else:
            questions = [q for q in rag_questions if q]
            if not questions:
                subject = state.pet_info.display_name if state.pet_info else "宠物"
                questions = [
                    "想先确认一下：这种情况持续多久了，大概多久出现一次呢？",
                    f"另外，{subject}目前精神和食欲怎么样，还有没有其他不舒服的表现呢？",
                ]

        # 物种未知：第一个问题先确认猫/狗
        if not species_known:
            questions = [species_question] + questions

        # 过滤空问题与历史已问过的问题（保序去重）
        asked = set(state.case_facts.asked_questions)
        filtered = list(dict.fromkeys(q for q in questions if q and q not in asked))
        if not filtered:
            # 用户未回答上一轮关键问题时，不能让 provisional 响应变成零追问；
            # 用更聚焦的重新确认措辞继续收集风险分级所需信息。
            filtered = [
                "为了判断是否需要尽快就医，想再确认一下：现在叫它时有反应、还能正常站立和走动吗？",
                "目前是否还愿意吃东西和喝水，有没有呕吐、腹泻或呼吸异常呢？",
            ]
        # 单次最多追问 2 个，避免问题清单式体验
        return filtered[:2]

    @staticmethod
    def _is_general_care_question(text: str) -> bool:
        """判断是否为日常护理频率类问题（洗澡/刷牙/剪指甲/梳毛周期）。

        这类问题不涉及症状与风险，信息本身完整，无需追问病史。

        :param text: 用户本轮文本
        :return: True 表示命中护理频率模式
        """
        # 去除空白后正则匹配（"多久洗一次澡""洗澡频率"等）
        normalized = re.sub(r"\s+", "", text or "")
        return any(pattern.search(normalized) for pattern in _GENERAL_CARE_PATTERNS)

    @staticmethod
    def _pet_resolved(state: ConsultState, text: str) -> bool:
        """多宠场景判断"问的是哪只"是否已明确。

        明确条件（任一）：
        1. 请求显式指定 pet_ref；
        2. 文本提到某只宠物的 name；
        3. 文本提到物种（狗/猫/犬/猫咪等），且宠物列表中该物种唯一。
        """
        if state.pet_ref:
            return True
        if not text:
            return False
        named = [p for p in state.pets if p.name and p.name in text]
        if len(named) == 1:
            return True
        # 物种唯一匹配
        species_hits = []
        for p in state.pets:
            s = (p.species or "").lower()
            if "狗" in text or "犬" in text:
                if s in ("dog", "犬", "狗", "狗狗"):
                    species_hits.append(p)
            if "猫" in text:
                if s in ("cat", "猫", "猫咪"):
                    species_hits.append(p)
        return len(species_hits) == 1
