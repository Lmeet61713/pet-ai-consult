from types import SimpleNamespace

from app.agent.consult_agent import ConsultAgent
from app.core.constants import AnswerMode, RiskLevel, VetUrgency
from app.rag.models import RagDecisionStatus, RagResult
from app.safety.medication_rules import MedicationRules
from app.schemas.consult import GeneratedConsultation, VetRecommendation
from app.schemas.pet import PetInfo
from app.services.medical_safety_service import MedicalSafetyService


def _generated(*, summary: str, answer_mode: AnswerMode = AnswerMode.PROVISIONAL):
    return GeneratedConsultation(
        summary=summary,
        possible_explanations=["饮食变化或轻度胃肠刺激"],
        what_to_do_now=["观察排便次数和性状", "提供清洁饮水"],
        risk_level=RiskLevel.LOW,
        answer_mode=answer_mode,
        vet_recommendation=VetRecommendation(
            recommended=False,
            urgency=VetUrgency.NONE,
        ),
    )


def _state(query: str, *, reason: str, generated=None, vision_findings=None):
    return SimpleNamespace(
        generated=generated or _generated(summary="狗狗拉稀两天，精神食欲正常。"),
        rag_result=RagResult(
            query=query,
            decision=RagDecisionStatus.INSUFFICIENT,
            reason_codes=[reason],
            index_version="test",
        ),
        rag_evidence=[],
        vision_findings=vision_findings or [],
        pet_info=PetInfo(species="dog"),
    )


def test_guard_does_not_overwrite_specific_diarrhea_answer():
    state = _state("狗拉稀怎么办", reason="low_relevance")
    assert ConsultAgent._apply_provisional_no_evidence_guard(state) is False
    assert state.generated.summary == "狗狗拉稀两天，精神食欲正常。"
    assert state.generated.possible_explanations


def test_guard_only_applies_to_true_vague_query():
    state = _state("狗狗状态不好", reason="vague_general_query")
    assert ConsultAgent._apply_provisional_no_evidence_guard(state) is True
    assert state.generated.possible_explanations == []
    assert "现有信息有限" in state.generated.summary


def test_guard_does_not_overwrite_image_findings():
    state = _state(
        "狗狗状态不好",
        reason="vague_general_query",
        vision_findings=[SimpleNamespace(observations=["左前肢擦伤"])],
    )
    assert ConsultAgent._apply_provisional_no_evidence_guard(state) is False


def test_weather_is_out_of_scope_without_pet_followup():
    assert ConsultAgent._is_out_of_scope_query("今天天气怎么样？") is True
    assert ConsultAgent._is_out_of_scope_query("天气热狗狗一直喘") is False
    assert ConsultAgent._is_out_of_scope_query("狗一般多久洗一次澡比较合适") is False


def test_local_safety_repair_preserves_targeted_fields():
    service = MedicalSafetyService(settings=None, checker=None)
    generated = _generated(summary="可以确定是胃肠炎，但仍需结合检查。")
    generated.answer_text = "可以确定是胃肠炎。"
    generated.what_to_do_now = [
        "记录腹泻次数、颜色和是否带血",
        "暂时禁食 4 小时",
        "随后继续正常喂食",
    ]

    repaired = service.repair_locally(
        generated,
        violations=["出现确诊式断言", "处置建议互相矛盾"],
        expected_risk=RiskLevel.LOW,
        expected_urgency=VetUrgency.NONE,
    )

    assert repaired is not None
    assert repaired.answer_text == ""
    assert "可以确定" not in repaired.summary
    assert "记录腹泻次数、颜色和是否带血" in repaired.what_to_do_now
    assert all("禁食" not in item for item in repaired.what_to_do_now)


def test_out_of_scope_response_has_no_medical_questions():
    state = SimpleNamespace(request_id="r1", conversation_id="c1")
    response = ConsultAgent._fixed_out_of_scope_response(state)
    assert response.follow_up_questions == []
    assert response.vet_recommendation.recommended is False
    assert response.risk_flags == ["out_of_scope"]


def test_symptom_frequency_is_not_treated_as_medication_dose():
    rules = MedicationRules()
    assert rules.violations("狗狗一天拉三次，建议每天记录两次排便情况。") == []
    assert "输出了用药频率或疗程" in rules.violations("建议每天服药 2 次。")
