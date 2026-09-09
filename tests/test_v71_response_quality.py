from types import SimpleNamespace

from app.agent.completeness_checker import CompletenessChecker
from app.agent.consult_agent import ConsultAgent
from app.core.constants import AnswerMode, RiskLevel, VetUrgency
from app.rag.hybrid_retriever import HybridRetriever
from app.rag.loader import RagAssetReport
from app.rag.models import RagDecisionStatus
from app.rag.retriever import ShadowRetriever, is_cat_chin_specific_query
from app.schemas.consult import GeneratedConsultation, VetRecommendation
from app.schemas.pet import PetInfo
from app.services.medical_safety_service import MedicalSafetyService


def _question_state(species: str = "dog"):
    return SimpleNamespace(
        pet_info=PetInfo(species=species),
        case_facts=SimpleNamespace(asked_questions=[]),
    )


def test_short_diarrhea_uses_symptom_specific_questions():
    questions = CompletenessChecker._thin_questions(
        _question_state("dog"),
        text="狗拉稀怎么办",
        rag_questions=["犬的年龄是多大？疫苗接种是否完成？"],
    )

    assert len(questions) == 2
    assert "一天大概几次" in questions[0]
    assert "鲜血或黑色柏油样" in questions[0]
    assert "精神和食欲" in questions[1]
    assert all("疫苗" not in question for question in questions)


def test_cat_chin_uses_local_questions_not_flea_card_question():
    questions = CompletenessChecker._thin_questions(
        _question_state("cat"),
        text="猫下巴有黑色颗粒，皮肤有结痂，掉毛",
        rag_questions=["腰背部或尾根的丘疹、结痂是否有扩大？"],
    )

    assert "下巴的黑色颗粒" in questions[0]
    assert "食盆和水盆" in questions[1]
    assert all("腰背部" not in question and "尾根" not in question for question in questions)


def test_general_care_question_is_complete():
    assert CompletenessChecker._is_general_care_question(
        "狗一般多久洗一次澡比较合适"
    )
    assert not CompletenessChecker._is_general_care_question("狗洗澡后皮肤发红")


def test_general_care_does_not_merge_model_health_followups():
    state = SimpleNamespace(
        completeness=SimpleNamespace(reason="general_care", questions=[]),
        case_facts=SimpleNamespace(domain="unknown", asked_questions=[]),
    )

    questions = ConsultAgent._merge_questions(
        state,
        ["这种情况持续多久了？", "狗狗目前精神和食欲怎么样？"],
    )

    assert questions == []


def test_cat_chin_query_is_not_grounded_by_unrelated_card():
    report = RagAssetReport(
        cards=({"id": "V17-CAT-DERM-001", "species": ["cat"]},),
        source_ids=frozenset(),
        index_version="test",
    )

    assert is_cat_chin_specific_query(
        "猫下巴有黑色颗粒，皮肤有结痂，掉毛", "cat"
    )
    for retriever in (
        ShadowRetriever(report),
        HybridRetriever(report, model_path=""),
    ):
        result = retriever.search(
            "猫下巴有黑色颗粒，皮肤有结痂，掉毛", species="cat"
        )
        assert result.decision is RagDecisionStatus.INSUFFICIENT
        assert result.hits == []
        assert "cat_chin_specific_no_matching_card" in result.reason_codes


def test_owner_facing_cleanup_removes_mixed_language_and_unreliable_checks():
    generated = GeneratedConsultation(
        summary="出现 lethargy，需要继续观察 appetite。",
        what_to_do_now=[
            "暂时停止喂食固体食物 4-6 小时，让肠胃休息。",
            "轻轻提起颈部皮肤，如果回弹超过 2 秒，说明脱水。",
        ],
        what_to_monitor=["耳尖或脚垫温度过高可能表示发烧。"],
        answer_mode=AnswerMode.PROVISIONAL,
        risk_level=RiskLevel.LOW,
        vet_recommendation=VetRecommendation(
            recommended=False,
            urgency=VetUrgency.NONE,
        ),
    )

    cleaned, changed = MedicalSafetyService.clean_owner_facing_language(generated)

    combined = " ".join(
        [cleaned.summary, *cleaned.what_to_do_now, *cleaned.what_to_monitor]
    )
    assert changed
    assert "lethargy" not in combined
    assert "appetite" not in combined
    assert "精神萎靡" in combined
    assert "不要自行禁食" in combined
    assert "若怀疑脱水" in combined
    assert "不要只凭耳朵或脚垫温度判断" in combined
