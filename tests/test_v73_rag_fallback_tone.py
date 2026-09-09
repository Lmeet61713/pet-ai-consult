from app.agent.completeness_checker import CompletenessChecker
from app.agent.consult_agent import ConsultAgent
from app.agent.state import ConsultState
from app.clients.knowledge_consult_client import DeepSeekOfficialAdapter
from app.rag.hybrid_retriever import HybridRetriever
from app.rag.loader import RagAssetReport
from app.rag.models import RagDecisionStatus
from app.rag.retriever import ShadowRetriever, is_ambiguous_elimination_query
from app.core.constants import AnswerMode, RiskLevel, VetUrgency
from app.schemas.consult import GeneratedConsultation, KnowledgeConsultRequest, VetRecommendation
from app.schemas.pet import PetInfo


def _state(text: str) -> ConsultState:
    return ConsultState(
        request_id="req-test",
        tenant_id="tenant-test",
        user_id="user-test",
        conversation_id="conversation-test",
        text=text,
        pet_info=PetInfo(species="dog"),
    )


def _report() -> RagAssetReport:
    # 故意只提供与问题无关的口腔卡，验证歧义问题不会被强行命中。
    card = {
        "id": "V17-DOG-ORAL-001",
        "title": "狗狗流口水",
        "species": ["dog"],
        "category": "oral",
        "retrieval_text": "狗狗流口水 口腔 异物 中毒 呕吐",
        "user_phrases": ["狗狗一直流口水"],
        "facts": [],
        "source_supported_simple_facts": ["流口水需要结合口腔情况判断"],
        "questions_to_ask": ["最近是否接触清洁剂？"],
    }
    return RagAssetReport((card,), frozenset(), "test")


def test_ambiguous_elimination_query_is_not_forced_into_rag_card() -> None:
    query = "小狗经常上厕所，这是什么原因？"
    assert is_ambiguous_elimination_query(query) is True
    assert is_ambiguous_elimination_query("小狗最近频繁排尿") is False
    assert is_ambiguous_elimination_query("小狗最近频繁拉稀") is False

    for retriever in (ShadowRetriever(_report()), HybridRetriever(_report())):
        result = retriever.search(query, species="dog")
        assert result.decision is RagDecisionStatus.INSUFFICIENT
        assert result.reason_codes == ["ambiguous_elimination"]
        assert result.hits == []


def test_ambiguous_elimination_gets_natural_clarifying_questions() -> None:
    result = CompletenessChecker().evaluate(
        _state("小狗经常上厕所，这是什么原因？"), rag_questions=["最近是否接触清洁剂？"]
    )
    assert result.need_more_info is True
    assert result.reason == "keyword_thin"
    assert "频繁小便还是频繁大便" in result.questions[0]
    assert all("清洁剂" not in question for question in result.questions)


def test_rag_miss_prompt_allows_9b_general_knowledge_answer() -> None:
    request = KnowledgeConsultRequest(
        request_id="req-test",
        user_question="小狗经常上厕所，这是什么原因？",
        pet_info={"species": "dog"},
        missing_information=["是频繁排尿还是频繁排便"],
        rag_evidence=[],
        rag_decision="insufficient",
        answer_mode="provisional",
    )
    rendered = DeepSeekOfficialAdapter._render_user(request)
    assert "使用你掌握的通用宠物健康知识正常回答" in rendered
    assert "不得把‘没有知识卡’等同于‘无法回答’" in rendered
    assert "不要退化成只有就医建议的短模板" in rendered


def test_structured_fallback_uses_conversational_rendering() -> None:
    generated = GeneratedConsultation(
        summary="小狗频繁上厕所，需要先区分是小便还是大便",
        visible_findings=["频繁上厕所"],
        possible_explanations=["饮食变化", "环境紧张", "泌尿道或肠道受到刺激"],
        what_to_do_now=["记录每次的种类、次数和量", "保持饮食和饮水规律"],
        avoid_actions=["自行使用人用止泻药或消炎药"],
        what_to_monitor=["有没有血色或疼痛", "精神和食欲是否变化"],
        follow_up_questions=["是频繁小便还是频繁大便呢？"],
        risk_level=RiskLevel.MEDIUM,
        answer_mode=AnswerMode.PROVISIONAL,
        vet_recommendation=VetRecommendation(
            recommended=True,
            urgency=VetUrgency.BOOK_VET,
            reason="如果出现血色、疼痛或精神食欲下降，请及时联系兽医",
        ),
    )
    answer = ConsultAgent._render(generated)
    assert "已确认的情况" not in answer
    assert "可能的原因（仅供参考）" not in answer
    assert "现在可以做的" not in answer
    assert "应避免" not in answer
    assert "常见可以从这几个方向考虑" in answer
    assert "您现在可以先这样做" in answer
    assert "接下来重点留意" in answer
